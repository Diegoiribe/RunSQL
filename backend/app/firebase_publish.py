from __future__ import annotations

import json
import os
import re
import unicodedata
from hashlib import sha1
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import duckdb
import pandas as pd

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "capacitaciones-api")
CHUNK_TARGET_BYTES = 600_000
# A Firestore commit is also limited by request size. With documents chunked at
# ~600 KB, ten writes keep the batch comfortably below that ceiling.
MAX_BATCH_OPERATIONS = 10


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
    return slugify(label), label


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
    views_collection = category_ref.collection("view_chunks")
    pending_collection = category_ref.collection("pending_chunks")

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

    for snapshot in views_collection.stream():
        if snapshot.id not in current_view_ids:
            operations.append(("delete", snapshot.reference, None))
    for snapshot in pending_collection.stream():
        if snapshot.id not in current_pending_ids:
            operations.append(("delete", snapshot.reference, None))

    # Remove fragments created by schema v1 after its replacement is ready.
    legacy_metrics = category_ref.collection("metric_chunks")
    for snapshot in legacy_metrics.stream():
        operations.append(("delete", snapshot.reference, None))

    written = _commit_operations(client, operations)
    metadata = {
        key: value
        for key, value in dataset.items()
        if key not in {"metrics", "views", "pending_rows"}
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
            "pending_sections": pending_sections,
            "schema_version": 3,
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
        "metric_rows": len(dataset["metrics"]),
        "pending_rows": len(dataset["pending_rows"]),
        "view_documents": sum(view_counts.values()),
        "detail_documents": len(pending_chunks),
        "documents_written": written + 3,
    }
