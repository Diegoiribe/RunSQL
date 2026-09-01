from __future__ import annotations

import json
import os
import re
import unicodedata
from hashlib import sha1
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Iterable, Iterator

import duckdb
import pandas as pd

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "capacitaciones-api")
_FIREBASE_APP_LOCK = Lock()
CHUNK_TARGET_BYTES = 600_000
# A Firestore commit is also limited by request size. With documents chunked at
# ~600 KB, ten writes keep the batch comfortably below that ceiling.
MAX_BATCH_OPERATIONS = 10

# Excepciones históricas explícitas. La categoría pública continúa siendo la
# clave del archivo SQL, aunque el proceso conserve otro sufijo internamente.
RESULT_TABLE_ALIASES = {
    "gerente_zona": ("resultados_capacitacion_gerente",),
}


class FirebaseConfigurationError(RuntimeError):
    pass


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def category_from_filename(filename: str) -> tuple[str, str]:
    label = re.sub(r"\s+ejemplo$", "", Path(filename).stem, flags=re.IGNORECASE).strip()
    category = slugify(label)
    if category == "encuesta_de_satisfaccion":
        label = "Encuesta de satisfacción"
    return category, label


def validate_cutoff_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("La fecha debe usar el formato AAAA-MM-DD.") from error
    if parsed.year < 2000 or parsed.year > 2100:
        raise ValueError("La fecha de corte está fuera del rango permitido.")
    return parsed


def apply_cutoff_date(sql: str, cutoff_date: str) -> str:
    parsed = validate_cutoff_date(cutoff_date)
    parameter = re.search(
        r"create\s+or\s+replace\s+table\s+parametros_[a-zA-Z0-9_]+\s+as\s+"
        r"select\s+date\s*'(?P<date>\d{4}-\d{2}-\d{2})'\s+as\s+fecha_corte",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not parameter:
        raise ValueError(
            "El SQL no contiene una tabla parametros_<categoria> con fecha_corte."
        )
    previous = parameter.group("date")
    return re.sub(
        rf"\bDATE\s*'{re.escape(previous)}'",
        f"DATE '{parsed.isoformat()}'",
        sql,
        flags=re.IGNORECASE,
    )


def result_table_name(connection: duckdb.DuckDBPyConnection, category: str) -> str:
    expected = f"resultados_capacitacion_{category}"
    names = {
        row[0]
        for row in connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """
        ).fetchall()
    }
    if expected in names:
        return expected
    for alias in RESULT_TABLE_ALIASES.get(category, ()):
        if alias in names:
            return alias
    candidates = sorted(
        name
        for name in names
        if name.startswith("resultados_capacitacion_")
        and category in name
    )
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"No se encontró la tabla {expected}. El SQL debe generar su tabla de resultados normalizada."
    )


def _safe(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _records(cursor: duckdb.DuckDBPyConnection) -> Iterator[dict]:
    columns = [description[0] for description in cursor.description]
    while True:
        rows = cursor.fetchmany(2_000)
        if not rows:
            return
        for row in rows:
            yield {
                column: _safe(value)
                for column, value in zip(columns, row)
            }


def _chunks(rows: Iterable[dict]) -> Iterator[list[dict]]:
    chunk: list[dict] = []
    chunk_bytes = 2
    for row in rows:
        row_bytes = len(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) + 1
        if chunk and chunk_bytes + row_bytes > CHUNK_TARGET_BYTES:
            yield chunk
            chunk = []
            chunk_bytes = 2
        chunk.append(row)
        chunk_bytes += row_bytes
    if chunk:
        yield chunk


def _tabular_payload(rows: list[dict]) -> dict:
    """Store a compact table without arrays nested inside another array.

    Firestore rejects ``[[row], [row]]``. Values are therefore flattened in
    row-major order and reconstructed by readers using the column count.
    """
    columns = list(rows[0]) if rows else []
    values = []
    for row in rows:
        for column in columns:
            value = row.get(column)
            if isinstance(value, (list, tuple, dict)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            values.append(value)
    return {
        "columns": columns,
        "row_count": len(rows),
        "values": values,
    }


def decode_tabular_payload(payload: dict) -> list[dict]:
    """Decode both the current flat payload and the legacy nested-row format."""
    columns = list(payload.get("columns") or [])
    legacy_rows = list(payload.get("rows") or [])
    if legacy_rows:
        rows = legacy_rows
    elif columns:
        values = list(payload.get("values") or [])
        width = len(columns)
        rows = [values[index : index + width] for index in range(0, len(values), width)]
    else:
        rows = []
    return [
        {column: row[index] if index < len(row) else None for index, column in enumerate(columns)}
        for row in rows
    ]


def _summarize_metrics(rows: Iterable[dict], dimensions: tuple[str, ...]) -> list[dict]:
    """Pre-aggregate a dashboard view so the reader never scans collaborator rows."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row[dimension] for dimension in dimensions)
        item = groups.setdefault(
            key,
            {
                **{dimension: row[dimension] for dimension in dimensions},
                "total": 0,
                "completados": 0,
            },
        )
        item["total"] += row["total"]
        item["completados"] += row["completados"]

    result = []
    for item in groups.values():
        item["pendientes"] = item["total"] - item["completados"]
        item["avance"] = (
            round(100.0 * item["completados"] / item["total"], 2)
            if item["total"]
            else 0.0
        )
        result.append(item)
    return sorted(result, key=lambda item: tuple(str(item[key]) for key in dimensions))


COMMENT_THEMES = (
    ("Explicación clara", ("clar", "explica", "comprend", "comunic")),
    ("Curso valioso", ("excelente", "buen curso", "recom", "interesante", "util")),
    ("Dominio del tema", ("dominio", "conocimiento", "preparad", "experiencia")),
    ("Dinámica y participación", ("dinamic", "interactiv", "particip", "actividad", "practic")),
    ("Atención y dudas", ("duda", "atencion", "apoyo", "disponib", "amable")),
    ("Equipo y materiales", ("equipo", "material", "herramient", "instalacion", "computadora")),
    ("Duración y ritmo", ("tiempo", "duracion", "rapido", "lento", "ritmo")),
    ("Modalidad", ("presencial", "virtual", "linea", "remoto")),
)
POSITIVE_COMMENT_WORDS = (
    "excelente", "bueno", "muy bien", "claro", "dinamic", "practic", "agradable",
    "gracias", "recomiendo", "felicidades", "aprendi", "util", "ameno",
    "amena", "participativo", "participativa", "gran talento", "ideal",
    "conocedor", "dominio", "atento", "atenta", "entusiasmad", "me agrado",
    "mejor forma", "agradec", "buen ", "carisma", "inspira", "confianza",
)
NEGATIVE_COMMENT_PATTERNS = (
    r"\bfalta(?:n|ba)?\b", r"\bhace falta\b",
    r"\bdeb(?:e|en|eria|erian) (?:mejorar|tener|agregar|incluir|cambiar)\b",
    r"\bse deb(?:e|eria) (?:mejorar|agregar|incluir|cambiar)\b",
    r"\bnecesita(?:n|mos)?\b", r"\bno funciona(?:n)?\b", r"\bmal estado\b",
    r"\bproblema(?:s)?\b", r"\bdeficiente\b", r"\bconfus[oa]\b",
    r"\b(?:demasiado|excesivamente) (?:rapido|lento)\b",
    r"\b(?:explica|habla|avanza|va|ritmo) muy (?:rapido|lento)\b",
    r"\bpoco tiempo\b", r"\bmas practica\b",
    r"\bpuede(?:n)? mejorar\b", r"\bpodria(?:n)? mejorar\b", r"\bmas equipos?\b",
    r"\bmejorar (?:el|la|los|las) (?:equipo|equipos|material|materiales|instalacion|instalaciones|contenido|curso|audio|computadora|computadoras)\b",
    r"\bno (?:fue|es|esta|estuvo|me parecio )?(?:bueno|claro|util|dinamic[oa]|practic[oa]|ameno|amena)\b",
    r"\bno (?:explica|comunica|funciona|ayuda|resuelve|cumple)\b",
    r"\bnunca (?:explica|comunica|resuelve|contesta|ayuda)\b",
    r"\bdificil de (?:entender|seguir|comprender)\b",
)


def _comment_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def _comment_labels(value: str) -> tuple[str, str]:
    normalized = _comment_key(value)
    theme = next(
        (label for label, words in COMMENT_THEMES if any(word in normalized for word in words)),
        "Comentario general",
    )
    positive_score = sum(word in normalized for word in POSITIVE_COMMENT_WORDS)
    negative_score = sum(bool(re.search(pattern, normalized)) for pattern in NEGATIVE_COMMENT_PATTERNS)
    if negative_score:
        sentiment = "negative"
    elif positive_score:
        sentiment = "positive"
    else:
        sentiment = "neutral"
    if theme == "Comentario general" and sentiment == "negative":
        theme = "Mejora general"
    elif theme == "Comentario general" and sentiment == "positive":
        theme = "Valoración positiva"
    return theme, sentiment


def _build_comment_details(rows: list[dict]) -> list[dict]:
    aggregates: dict[tuple, dict] = {}
    recent: list[dict] = []
    for row in rows:
        comment = str(row.get("comentario") or "").strip()
        theme, sentiment = _comment_labels(comment)
        month = str(row.get("fecha") or "")[:7]
        dimensions = (
            month, row["programa"], row["curso"], row["instructor"],
            row["region"], sentiment, theme,
        )
        item = aggregates.setdefault(
            dimensions,
            {
                "record_type": "theme", "fecha": None, "mes": month,
                "programa": row["programa"], "curso": row["curso"],
                "instructor": row["instructor"], "region": row["region"],
                "recomendacion": None, "sentiment": sentiment, "theme": theme,
                "count": 0, "comentario": None, "example": comment,
            },
        )
        item["count"] += 1
        if len(recent) < 1500:
            recent.append(
                {
                    "record_type": "comment", "fecha": row["fecha"], "mes": month,
                    "programa": row["programa"], "curso": row["curso"],
                    "instructor": row["instructor"], "region": row["region"],
                    "recomendacion": row["recomendacion"], "sentiment": sentiment,
                    "theme": theme, "count": 1, "comentario": comment,
                    "example": comment,
                }
            )
    themes = sorted(aggregates.values(), key=lambda item: (-item["count"], item["theme"]))
    return themes + recent


def build_dashboard_dataset(
    connection: duckdb.DuckDBPyConnection,
    category: str,
    category_label: str,
    cutoff: date,
) -> dict:
    table = result_table_name(connection, category)
    quoted_table = table.replace('"', '""')
    included = (
        "LOWER(COALESCE(CAST(iniciativa AS VARCHAR), 'no')) "
        "NOT IN ('si', 'sí')"
    )
    metrics_cursor = connection.execute(
        f"""
        SELECT
            COALESCE(CAST(nombre_puesto AS VARCHAR), 'Sin puesto') AS puesto,
            COALESCE(CAST(region AS VARCHAR), 'Sin región') AS region,
            COALESCE(CAST(curso AS VARCHAR), 'Sin curso') AS curso,
            CAST(SUM(COALESCE(total, 0)) AS BIGINT) AS total,
            CAST(SUM(COALESCE(completados, 0)) AS BIGINT) AS completados
        FROM "{quoted_table}"
        WHERE {included}
        GROUP BY puesto, region, curso
        ORDER BY puesto, region, curso
        """
    )
    metrics = list(_records(metrics_cursor))
    for row in metrics:
        row["pendientes"] = row["total"] - row["completados"]
        row["avance"] = round(
            100.0 * row["completados"] / row["total"], 2
        ) if row["total"] else 0.0

    pending_cursor = connection.execute(
        f"""
        SELECT
            CAST(numero_persona AS VARCHAR) AS numero_persona,
            COALESCE(CAST(nombre AS VARCHAR), '') AS nombre,
            COALESCE(CAST(nombre_puesto AS VARCHAR), 'Sin puesto') AS puesto,
            COALESCE(CAST(region AS VARCHAR), 'Sin región') AS region,
            CAST(tienda AS VARCHAR) AS tienda,
            COALESCE(CAST(curso AS VARCHAR), 'Sin curso') AS curso
        FROM "{quoted_table}"
        WHERE {included}
          AND COALESCE(completados, 0) < COALESCE(total, 0)
        ORDER BY region, nombre, numero_persona, curso
        """
    )
    pending = list(_records(pending_cursor))
    total = sum(row["total"] for row in metrics)
    completed = sum(row["completados"] for row in metrics)
    pending_total = total - completed
    progress = round(100.0 * completed / total, 2) if total else 0.0
    period = cutoff.strftime("%Y-%m")
    return {
        "data_kind": "training",
        "period": period,
        "category": category,
        "category_label": category_label,
        "cutoff_date": cutoff.isoformat(),
        "year": cutoff.year,
        "month": cutoff.month,
        "total": total,
        "completed": completed,
        "pending": pending_total,
        "progress_percentage": progress,
        "pending_percentage": round(100.0 - progress, 2),
        "positions": sorted({row["puesto"] for row in metrics}),
        "regions": sorted({row["region"] for row in metrics}),
        "courses": sorted({row["curso"] for row in metrics}),
        "metrics": metrics,
        "views": {
            "positions": _summarize_metrics(metrics, ("puesto",)),
            "regions": _summarize_metrics(metrics, ("region",)),
            "courses": _summarize_metrics(metrics, ("curso",)),
            "course_regions": _summarize_metrics(metrics, ("curso", "region")),
        },
        "pending_rows": pending,
    }


def build_eic_dashboard_dataset(
    connection: duckdb.DuckDBPyConnection,
    category: str,
    category_label: str,
    cutoff: date,
) -> dict:
    """Package the administrative EIC model without flattening its finances."""
    required_tables = {
        "eic_resumen_general",
        "eic_resumen_c_level",
        "eic_resumen_direccion",
        "eic_resumen_iniciativa",
        "eic_estatus_cotizaciones_direccion",
        "eic_estatus_capacitaciones_direccion",
        "eic_estatus_pagos_direccion",
        "eic_capacitaciones",
        "eic_pagos",
        "eic_controles",
    }
    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    missing = sorted(required_tables - existing_tables)
    if missing:
        raise ValueError(
            "El SQL de EIC no generó las tablas requeridas: " + ", ".join(missing)
        )

    def table_rows(table: str) -> list[dict]:
        quoted = table.replace('"', '""')
        return list(_records(connection.execute(f'SELECT * FROM "{quoted}"')))

    general = table_rows("eic_resumen_general")
    c_level = table_rows("eic_resumen_c_level")
    directions = table_rows("eic_resumen_direccion")
    initiatives = table_rows("eic_resumen_iniciativa")

    # Keep the cube compatible with the existing reader while DataStore uses
    # the richer EIC views for its dedicated financial report.
    metrics = []
    for row in initiatives:
        completed = int(row.get("estatus_grupo_principal") == "Impartido")
        metrics.append(
            {
                "puesto": row.get("direccion_nivel_2") or "Sin dirección",
                "region": row.get("direccion_c_level") or "Sin dirección C-Level",
                "curso": row.get("nombre_iniciativa") or "Sin nombre",
                "total": 1,
                "completados": completed,
                "pendientes": 1 - completed,
                "avance": float(completed * 100),
                "identificador": row.get("identificador"),
            }
        )

    general_row = general[0] if general else {}
    budget = float(general_row.get("presupuesto_autorizado_mxn") or 0)
    investment = float(general_row.get("inversion_actual_mxn") or 0)
    remaining = float(
        general_row.get("presupuesto_por_ejercer_mxn")
        if general_row.get("presupuesto_por_ejercer_mxn") is not None
        else budget - investment
    )
    progress = round(
        100.0 * float(general_row.get("avance_presupuesto") or 0), 2
    ) if general else (round(100.0 * investment / budget, 2) if budget else 0.0)
    period = cutoff.strftime("%Y-%m")
    return {
        "data_kind": "eic_administrative",
        "period": period,
        "category": category,
        "category_label": category_label,
        "cutoff_date": cutoff.isoformat(),
        "year": cutoff.year,
        "month": cutoff.month,
        "total": budget,
        "completed": investment,
        "pending": remaining,
        "progress_percentage": progress,
        "pending_percentage": round(100.0 - progress, 2),
        "positions": sorted({str(row["puesto"]) for row in metrics}),
        "regions": sorted({str(row["region"]) for row in metrics}),
        "courses": sorted({str(row["curso"]) for row in metrics}),
        "metrics": metrics,
        "views": {
            "general": general,
            "c_level": c_level,
            "directions": directions,
            "initiatives": initiatives,
            "quotation_status": table_rows("eic_estatus_cotizaciones_direccion"),
            "training_status": table_rows("eic_estatus_capacitaciones_direccion"),
            "payment_status": table_rows("eic_estatus_pagos_direccion"),
            "training_groups": table_rows("eic_capacitaciones"),
            "payments": table_rows("eic_pagos"),
            "controls": table_rows("eic_controles"),
        },
        "pending_rows": [],
        "detail_rows": [],
        "eic_views": [
            "general", "c_level", "directions", "initiatives", "quotation_status",
            "training_status", "payment_status", "training_groups", "payments",
            "controls",
        ],
    }


def build_satisfaction_dashboard_dataset(
    connection: duckdb.DuckDBPyConnection,
    category: str,
    category_label: str,
    cutoff: date,
) -> dict:
    """Aggregate the canonical satisfaction table into a compact dashboard cube."""
    names = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    specific_table = f"resultados_satisfaccion_{category}"
    table = specific_table if specific_table in names else "resultados_satisfaccion"
    if table not in names:
        raise ValueError(
            "No se encontró resultados_satisfaccion. El SQL debe generar la tabla canónica de encuestas."
        )
    quoted_table = table.replace('"', '""')
    cutoff_text = cutoff.isoformat()
    metrics = list(
        _records(
            connection.execute(
                f"""
                SELECT
                    STRFTIME(fecha, '%Y-%m') AS mes,
                    COALESCE(NULLIF(TRIM(CAST(programa AS VARCHAR)), ''), 'Sin programa') AS programa,
                    COALESCE(NULLIF(TRIM(CAST(curso AS VARCHAR)), ''), 'Sin curso') AS curso,
                    COALESCE(NULLIF(TRIM(CAST(instructor AS VARCHAR)), ''), 'Sin instructor') AS instructor,
                    COALESCE(NULLIF(TRIM(CAST(region AS VARCHAR)), ''), 'Sin región') AS region,
                    COUNT(*)::BIGINT AS respuestas,
                    SUM(CASE WHEN dominio BETWEEN 1 AND 5 THEN dominio ELSE 0 END)::DOUBLE AS dominio_suma,
                    COUNT(CASE WHEN dominio BETWEEN 1 AND 5 THEN 1 END)::BIGINT AS dominio_n,
                    SUM(CASE WHEN comunicacion BETWEEN 1 AND 5 THEN comunicacion ELSE 0 END)::DOUBLE AS comunicacion_suma,
                    COUNT(CASE WHEN comunicacion BETWEEN 1 AND 5 THEN 1 END)::BIGINT AS comunicacion_n,
                    SUM(CASE WHEN interes BETWEEN 1 AND 5 THEN interes ELSE 0 END)::DOUBLE AS interes_suma,
                    COUNT(CASE WHEN interes BETWEEN 1 AND 5 THEN 1 END)::BIGINT AS interes_n,
                    SUM(CASE WHEN participacion BETWEEN 1 AND 5 THEN participacion ELSE 0 END)::DOUBLE AS participacion_suma,
                    COUNT(CASE WHEN participacion BETWEEN 1 AND 5 THEN 1 END)::BIGINT AS participacion_n,
                    SUM(CASE WHEN resolucion BETWEEN 1 AND 5 THEN resolucion ELSE 0 END)::DOUBLE AS resolucion_suma,
                    COUNT(CASE WHEN resolucion BETWEEN 1 AND 5 THEN 1 END)::BIGINT AS resolucion_n,
                    COUNT(CASE WHEN recomendacion BETWEEN 0 AND 10 THEN 1 END)::BIGINT AS nps_validas,
                    COUNT(CASE WHEN recomendacion BETWEEN 9 AND 10 THEN 1 END)::BIGINT AS promotores,
                    COUNT(CASE WHEN recomendacion BETWEEN 7 AND 8 THEN 1 END)::BIGINT AS pasivos,
                    COUNT(CASE WHEN recomendacion BETWEEN 0 AND 6 THEN 1 END)::BIGINT AS detractores,
                    COUNT(CASE WHEN recomendacion = 5 THEN 1 END)::BIGINT AS respuestas_cinco,
                    MIN(fecha)::DATE AS primera_respuesta,
                    MAX(fecha)::DATE AS ultima_respuesta
                FROM "{quoted_table}"
                WHERE fecha IS NOT NULL AND fecha::DATE <= DATE '{cutoff_text}'
                GROUP BY mes, programa, curso, instructor, region
                ORDER BY mes, programa, curso, instructor, region
                """
            )
        )
    )
    sum_fields = ("dominio_suma", "comunicacion_suma", "interes_suma", "participacion_suma", "resolucion_suma")
    count_fields = ("dominio_n", "comunicacion_n", "interes_n", "participacion_n", "resolucion_n")
    response_count = sum(int(row["respuestas"]) for row in metrics)
    rubric_sum = sum(float(row[field] or 0) for row in metrics for field in sum_fields)
    rubric_count = sum(int(row[field] or 0) for row in metrics for field in count_fields)
    promoters = sum(int(row["promotores"]) for row in metrics)
    detractors = sum(int(row["detractores"]) for row in metrics)
    nps_valid = sum(int(row["nps_validas"]) for row in metrics)
    score_fives = sum(int(row["respuestas_cinco"]) for row in metrics)
    isa = round(100.0 * rubric_sum / (5 * rubric_count), 2) if rubric_count else 0.0
    nps = round(100.0 * (promoters - detractors) / nps_valid, 2) if nps_valid else 0.0
    raw_comments = list(
        _records(
            connection.execute(
                f"""
                SELECT fecha::DATE AS fecha,
                       COALESCE(NULLIF(TRIM(CAST(programa AS VARCHAR)), ''), 'Sin programa') AS programa,
                       COALESCE(NULLIF(TRIM(CAST(curso AS VARCHAR)), ''), 'Sin curso') AS curso,
                       COALESCE(NULLIF(TRIM(CAST(instructor AS VARCHAR)), ''), 'Sin instructor') AS instructor,
                       COALESCE(NULLIF(TRIM(CAST(region AS VARCHAR)), ''), 'Sin región') AS region,
                       recomendacion, TRIM(comentario) AS comentario
                FROM "{quoted_table}"
                WHERE fecha IS NOT NULL AND fecha::DATE <= DATE '{cutoff_text}'
                  AND comentario IS NOT NULL AND LENGTH(TRIM(comentario)) >= 4
                  AND LOWER(TRIM(comentario)) NOT IN ('ninguno', 'ninguna', 'no aplica', 'n/a')
                ORDER BY fecha DESC
                """
            )
        )
    )
    period = cutoff.strftime("%Y-%m")
    return {
        "data_kind": "satisfaction",
        "period": period, "category": category, "category_label": category_label,
        "cutoff_date": cutoff.isoformat(), "year": cutoff.year, "month": cutoff.month,
        "total": rubric_count * 5, "completed": round(rubric_sum, 4),
        "pending": round((rubric_count * 5) - rubric_sum, 4),
        "progress_percentage": isa, "pending_percentage": round(100.0 - isa, 2),
        "response_count": response_count, "isa": isa, "nps": nps,
        "nps_valid_responses": nps_valid, "nps_promoter_responses": promoters,
        "nps_detractor_responses": detractors, "score_five_responses": score_fives,
        "nps_scale_status": "conventional_0_10",
        "programs": sorted({str(row["programa"]) for row in metrics}),
        "courses": sorted({str(row["curso"]) for row in metrics}),
        "instructors": sorted({str(row["instructor"]) for row in metrics}),
        "regions": sorted({str(row["region"]) for row in metrics}),
        "positions": [], "metrics": metrics, "views": {}, "pending_rows": [],
        "detail_rows": _build_comment_details(raw_comments),
    }


def _firebase_client():
    credential_value = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
    fallback = Path(__file__).resolve().parents[1] / "firebase-service-account.json"
    if not credential_value and fallback.exists():
        credential_value = str(fallback)
    if not credential_value:
        raise FirebaseConfigurationError(
            "Falta la cuenta de servicio de Firebase. Descarga el JSON desde Firebase Console "
            "y guárdalo como backend/firebase-service-account.json. También puedes definir "
            "FIREBASE_SERVICE_ACCOUNT en .env con la ruta al archivo. No agregues la clave a Git."
        )

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        with _FIREBASE_APP_LOCK:
            try:
                firebase_app = firebase_admin.get_app("runsql")
            except ValueError:
                if credential_value.startswith("{"):
                    credential_data = json.loads(credential_value)
                    credential = credentials.Certificate(credential_data)
                else:
                    credential_path = Path(credential_value).expanduser().resolve()
                    if not credential_path.is_file():
                        raise FirebaseConfigurationError(
                            f"No existe la cuenta de servicio: {credential_path}"
                        )
                    credential = credentials.Certificate(str(credential_path))
                firebase_app = firebase_admin.initialize_app(
                    credential,
                    {"projectId": FIREBASE_PROJECT_ID},
                    name="runsql",
                )
        return firestore.client(app=firebase_app)
    except FirebaseConfigurationError:
        raise
    except Exception as error:
        raise FirebaseConfigurationError(
            f"No se pudo conectar con Firebase: {error}"
        ) from error


def _commit_operations(client, operations: list[tuple[str, object, dict | None]]) -> int:
    committed = 0
    for start in range(0, len(operations), MAX_BATCH_OPERATIONS):
        batch = client.batch()
        group = operations[start : start + MAX_BATCH_OPERATIONS]
        for action, reference, payload in group:
            if action == "set":
                batch.set(reference, payload)
            else:
                batch.delete(reference)
        batch.commit()
        committed += len(group)
    return committed


def publish_dashboard(dataset: dict) -> dict:
    try:
        return _publish_dashboard(dataset)
    except FirebaseConfigurationError:
        raise
    except Exception as error:
        raise FirebaseConfigurationError(
            f"Firebase rechazó la publicación: {error}"
        ) from error


def _publish_dashboard(dataset: dict) -> dict:
    client = _firebase_client()
    period_ref = client.collection("periods").document(dataset["period"])
    category_ref = period_ref.collection("categories").document(dataset["category"])
    existing_snapshot = category_ref.get()
    existing_metadata = (
        existing_snapshot.to_dict() or {}
        if getattr(existing_snapshot, "exists", False)
        else {}
    )
    replaced_existing = bool(existing_metadata)
    same_cutoff_replacement = (
        replaced_existing
        and existing_metadata.get("cutoff_date") == dataset["cutoff_date"]
    )
    publication_revision = int(existing_metadata.get("publication_revision") or 0) + 1
    views_collection = category_ref.collection("view_chunks")
    pending_collection = category_ref.collection("pending_chunks")
    detail_collection = category_ref.collection("detail_chunks")

    view_rows = {"cube": dataset["metrics"], **dataset["views"]}
    view_chunks = {
        view: list(_chunks(rows))
        for view, rows in view_rows.items()
    }
    pending_by_section: dict[tuple[str, str], list[dict]] = {}
    for row in dataset["pending_rows"]:
        section = (row["region"], row["puesto"])
        pending_by_section.setdefault(section, []).append(row)
    pending_chunks: list[tuple[str, str, list[dict]]] = []
    for region, position in sorted(pending_by_section):
        pending_chunks.extend(
            (region, position, rows)
            for rows in _chunks(pending_by_section[(region, position)])
        )
    operations: list[tuple[str, object, dict | None]] = []
    current_view_ids = set()
    current_pending_ids = set()
    current_detail_ids = set()

    for view, chunks in view_chunks.items():
        for index, rows in enumerate(chunks):
            document_id = f"{view}_{index:05d}"
            current_view_ids.add(document_id)
            operations.append(
                (
                    "set",
                    views_collection.document(document_id),
                    {
                        "view": view,
                        "index": index,
                        **_tabular_payload(rows),
                    },
                )
            )
    section_indexes: dict[tuple[str, str], int] = {}
    for region, position, rows in pending_chunks:
        section = (region, position)
        index = section_indexes.get(section, 0)
        section_indexes[section] = index + 1
        region_key = slugify(region)[:32] or "sin_region"
        position_key = slugify(position)[:32] or "sin_puesto"
        section_hash = sha1(
            f"{region}\0{position}".encode("utf-8")
        ).hexdigest()[:8]
        document_id = f"{region_key}_{position_key}_{section_hash}_{index:04d}"
        current_pending_ids.add(document_id)
        compact_rows = [
            {
                "numero_persona": row["numero_persona"],
                "nombre": row["nombre"],
                "tienda": row["tienda"],
                "curso": row["curso"],
            }
            for row in rows
        ]
        operations.append(
            (
                "set",
                pending_collection.document(document_id),
                {
                    "index": index,
                    "region": region,
                    "position": position,
                    "courses": sorted({row["curso"] for row in rows}),
                    "section_key": section_hash,
                    **_tabular_payload(compact_rows),
                },
            )
        )

    detail_chunks = list(_chunks(dataset.get("detail_rows") or []))
    for index, rows in enumerate(detail_chunks):
        document_id = f"detail_{index:05d}"
        current_detail_ids.add(document_id)
        operations.append(("set", detail_collection.document(document_id), {"index": index, **_tabular_payload(rows)}))

    for snapshot in views_collection.stream():
        if snapshot.id not in current_view_ids:
            operations.append(("delete", snapshot.reference, None))
    for snapshot in pending_collection.stream():
        if snapshot.id not in current_pending_ids:
            operations.append(("delete", snapshot.reference, None))
    for snapshot in detail_collection.stream():
        if snapshot.id not in current_detail_ids:
            operations.append(("delete", snapshot.reference, None))

    # Remove fragments created by schema v1 after its replacement is ready.
    legacy_metrics = category_ref.collection("metric_chunks")
    for snapshot in legacy_metrics.stream():
        operations.append(("delete", snapshot.reference, None))

    written = _commit_operations(client, operations)
    metadata = {
        key: value
        for key, value in dataset.items()
        if key not in {"metrics", "views", "pending_rows", "detail_rows"}
    }
    view_counts = {
        view: len(chunks)
        for view, chunks in view_chunks.items()
    }
    pending_sections = [
        {
            "region": region,
            "position": position,
            "section_key": sha1(
                f"{region}\0{position}".encode("utf-8")
            ).hexdigest()[:8],
            "chunks": section_indexes[(region, position)],
        }
        for region, position in sorted(pending_by_section)
    ]
    metadata.update(
        {
            "metric_rows": len(dataset["metrics"]),
            "pending_rows": len(dataset["pending_rows"]),
            "view_chunks": view_counts,
            "pending_chunks": len(pending_chunks),
            "detail_chunks": len(detail_chunks),
            "pending_sections": pending_sections,
            "schema_version": 4,
            "publication_revision": publication_revision,
            "publication_action": "replaced" if replaced_existing else "created",
            "updated_at": datetime.now(timezone.utc),
        }
    )
    category_ref.set(metadata)
    period_ref.set(
        {
            "period": dataset["period"],
            "year": dataset["year"],
            "month": dataset["month"],
            "updated_at": datetime.now(timezone.utc),
        },
        merge=True,
    )
    history_ref = client.collection("dashboard_categories").document(
        dataset["category"]
    )
    history_ref.set(
        {
            "category": dataset["category"],
            "category_label": dataset["category_label"],
            "collection_key": dataset.get("collection_key"),
            "collection_label": dataset.get("collection_label"),
            "periods": {
                dataset["period"]: {
                    "cutoff_date": dataset["cutoff_date"],
                    "year": dataset["year"],
                    "month": dataset["month"],
                    "total": dataset["total"],
                    "completed": dataset["completed"],
                    "pending": dataset["pending"],
                    "progress_percentage": dataset["progress_percentage"],
                    "pending_percentage": dataset["pending_percentage"],
                }
            },
            "updated_at": datetime.now(timezone.utc),
        },
        merge=True,
    )
    return {
        "project_id": FIREBASE_PROJECT_ID,
        "period": dataset["period"],
        "category": dataset["category"],
        "category_label": dataset["category_label"],
        "collection_key": dataset.get("collection_key"),
        "collection_label": dataset.get("collection_label"),
        "cutoff_date": dataset["cutoff_date"],
        "replaced_existing": replaced_existing,
        "same_cutoff_replacement": same_cutoff_replacement,
        "publication_revision": publication_revision,
        "metric_rows": len(dataset["metrics"]),
        "pending_rows": len(dataset["pending_rows"]),
        "view_documents": sum(view_counts.values()),
        "detail_documents": len(pending_chunks) + len(detail_chunks),
        "documents_written": written + 3,
    }
