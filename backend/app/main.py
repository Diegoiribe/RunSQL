from __future__ import annotations

import io
import os
import re
import unicodedata
import base64
from pathlib import Path

import duckdb
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .catalog import (
    FILE_CATALOG,
    FILE_FAMILIES,
    FileDefinition,
    FileFamily,
    build_catalog,
    definition_targets,
)

IS_VERCEL = bool(os.getenv("VERCEL"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "4" if IS_VERCEL else "200"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_RESULT_ROWS = 10_000
MAX_TOTAL_UPLOAD_MB = int(os.getenv("MAX_TOTAL_UPLOAD_MB", "4" if IS_VERCEL else "500"))
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}
BLOCKED_SQL = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|copy|attach|detach|"
    r"install|load|pragma|call|export|import|vacuum|read_csv|read_csv_auto|"
    r"read_json|read_json_auto|read_parquet|parquet_scan|glob|sqlite_scan|postgres_scan)\b",
    flags=re.IGNORECASE,
)
BLOCKED_SCRIPT_SQL = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|truncate|copy|attach|detach|"
    r"install|load|pragma|call|export|import|vacuum|read_csv|read_csv_auto|"
    r"read_json|read_json_auto|read_parquet|parquet_scan|glob|sqlite_scan|"
    r"postgres_scan|read_xlsx)\b",
    flags=re.IGNORECASE,
)

app = FastAPI(title="RunSQL API", version="0.1.0")
allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def match_definition(
    filename: str, definitions: tuple[FileDefinition, ...] = FILE_CATALOG
) -> FileDefinition | None:
    candidate = normalize_name(Path(filename).stem)
    for definition in definitions:
        accepted = {normalize_name(definition.name), normalize_name(definition.key)}
        accepted.update(normalize_name(alias) for alias in definition.aliases)
        if candidate in accepted:
            return definition
    return None


def match_family(
    filename: str, families: tuple[FileFamily, ...] = FILE_FAMILIES
) -> tuple[FileFamily, int] | None:
    candidate = normalize_name(Path(filename).stem)
    match = re.fullmatch(r"(.+?) p\s*(\d+)", candidate)
    if not match:
        return None
    prefix, period_text = match.groups()
    for family in families:
        if prefix in {normalize_name(value) for value in family.prefixes}:
            return family, int(period_text)
    return None


def validate_sql(sql: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.DOTALL).strip()
    statement = without_comments.rstrip(";").strip()
    if not statement:
        raise HTTPException(status_code=400, detail="La consulta SQL está vacía.")
    if ";" in statement:
        raise HTTPException(status_code=400, detail="Solo se permite una consulta por ejecución.")
    if not re.match(r"^(select|with)\b", statement, flags=re.IGNORECASE):
        raise HTTPException(status_code=400, detail="La consulta debe comenzar con SELECT o WITH.")
    if BLOCKED_SQL.search(statement):
        raise HTTPException(status_code=400, detail="La consulta contiene una instrucción no permitida.")
    return statement


def split_sql_script(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0

    while index < len(sql):
        character = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        current.append(character)

        if line_comment:
            if character == "\n":
                line_comment = False
        elif block_comment:
            if character == "*" and following == "/":
                current.append(following)
                index += 1
                block_comment = False
        elif quote:
            if character == quote:
                if following == quote:
                    current.append(following)
                    index += 1
                else:
                    quote = None
        elif character == "-" and following == "-":
            current.append(following)
            index += 1
            line_comment = True
        elif character == "/" and following == "*":
            current.append(following)
            index += 1
            block_comment = True
        elif character in ("'", '"'):
            quote = character
        elif character == ";":
            statement = "".join(current[:-1]).strip()
            if statement:
                statements.append(statement)
            current = []
        index += 1

    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


def without_sql_comments(statement: str) -> str:
    return re.sub(r"/\*.*?\*/|--[^\n]*", " ", statement, flags=re.DOTALL).strip()


def prepare_statements(
    sql: str,
    definitions: tuple[FileDefinition, ...] = FILE_CATALOG,
    families: tuple[FileFamily, ...] = FILE_FAMILIES,
) -> list[tuple[str, bool]]:
    source_tables = {
        target.table_name
        for definition in definitions
        for target in definition_targets(definition)
    }
    source_tables.update(item.table_name for item in families)
    prepared: list[tuple[str, bool]] = []

    for statement in split_sql_script(sql):
        clean = without_sql_comments(statement)
        if not clean:
            continue
        if re.match(r"^(install|load)\b", clean, flags=re.IGNORECASE):
            continue
        if re.match(r"^copy\b", clean, flags=re.IGNORECASE):
            continue

        source_match = re.match(
            r'^create\s+or\s+replace\s+table\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?\s+as\b',
            clean,
            flags=re.IGNORECASE,
        )
        if source_match and source_match.group(1).lower() in source_tables and re.search(
            r"\bread_xlsx\s*\(", clean, flags=re.IGNORECASE
        ):
            continue

        is_query = bool(re.match(r"^(select|with)\b", clean, flags=re.IGNORECASE))
        is_safe_ddl = bool(
            re.match(
                r"^create\s+or\s+replace\s+(table|view|macro)\b",
                clean,
                flags=re.IGNORECASE,
            )
        )
        if not is_query and not is_safe_ddl:
            raise HTTPException(
                status_code=400,
                detail="El archivo SQL contiene una instrucción no permitida para ejecución web.",
            )
        if BLOCKED_SCRIPT_SQL.search(clean):
            raise HTTPException(
                status_code=400,
                detail="El archivo SQL intenta acceder a archivos o modificar datos fuera del proceso permitido.",
            )
        prepared.append((statement, is_query))

    if not prepared:
        raise HTTPException(status_code=400, detail="El archivo SQL no contiene instrucciones ejecutables.")
    return prepared


def prepare_exports(sql: str) -> list[tuple[str, str, str]]:
    """Convert safe COPY (SELECT ...) TO '*.xlsx' statements into web downloads."""
    exports: list[tuple[str, str, str]] = []
    for statement in split_sql_script(sql):
        clean = without_sql_comments(statement)
        if not re.match(r"^copy\b", clean, flags=re.IGNORECASE):
            continue
        match = re.fullmatch(
            r"copy\s*\((.*)\)\s*to\s*'((?:''|[^'])+)'\s*(?:with\s*\((.*)\))?\s*",
            clean,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise HTTPException(
                status_code=400,
                detail="No se pudo interpretar una exportación COPY del archivo SQL.",
            )
        query, output_path, options = match.groups()
        query = query.strip()
        if not re.match(r"^(select|with)\b", query, flags=re.IGNORECASE):
            raise HTTPException(status_code=400, detail="Las exportaciones deben usar SELECT o WITH.")
        if BLOCKED_SCRIPT_SQL.search(query):
            raise HTTPException(status_code=400, detail="Una exportación contiene una instrucción no permitida.")
        filename = Path(output_path.replace("''", "'")).name
        if Path(filename).suffix.lower() != ".xlsx":
            raise HTTPException(status_code=400, detail="La aplicación web solo exporta archivos XLSX.")
        sheet_match = re.search(r"\bsheet\s+'((?:''|[^'])+)'", options or "", flags=re.IGNORECASE)
        sheet_name = sheet_match.group(1).replace("''", "'") if sheet_match else "Resultados"
        exports.append((query, filename, sheet_name[:31]))
    return exports


def read_dataframe(
    filename: str,
    payload: bytes,
    sheet_name: str | int = 0,
    range_name: str = "",
) -> pd.DataFrame:
    extension = Path(filename).suffix.lower()
    try:
        if extension == ".csv":
            try:
                return pd.read_csv(io.BytesIO(payload), encoding="utf-8-sig")
            except UnicodeDecodeError:
                return pd.read_csv(io.BytesIO(payload), encoding="latin-1")
        skiprows = None
        usecols = None
        if range_name:
            range_match = re.fullmatch(
                r"([A-Za-z]+)(\d+):([A-Za-z]+)(?:\d+)?", range_name.strip()
            )
            if not range_match:
                raise ValueError(f"el rango '{range_name}' no es compatible")
            first_column, first_row, last_column = range_match.groups()
            skiprows = max(int(first_row) - 1, 0)
            usecols = f"{first_column}:{last_column}"
        return pd.read_excel(
            io.BytesIO(payload),
            sheet_name=sheet_name,
            dtype=str,
            skiprows=skiprows,
            usecols=usecols,
        )
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"No se pudo leer {filename}. Verifica que el archivo no esté dañado: {error}",
        ) from error


def json_safe(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/catalog")
def catalog() -> dict:
    return catalog_payload((), ())


def catalog_payload(
    definitions: tuple[FileDefinition, ...], families: tuple[FileFamily, ...]
) -> dict:
    return {
        "files": [definition.public_dict() for definition in definitions],
        "families": [family.public_dict() for family in families],
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024),
        "max_total_upload_mb": MAX_TOTAL_UPLOAD_MB,
        "max_result_rows": MAX_RESULT_ROWS,
    }


@app.post("/api/analyze-sql")
def analyze_sql(sql: str = Form(...), filename: str = Form(...)) -> dict:
    definitions, families = build_catalog(sql, filename)
    return catalog_payload(definitions, families)


@app.post("/api/execute")
async def execute(
    sql: str = Form(...),
    files: list[UploadFile] = File(...),
    sql_filename: str = Form("consulta.sql"),
    rules_sql: str = Form(""),
    preview_table: str = Form(""),
    preview_limit: int = Form(100),
) -> dict:
    if not isinstance(sql_filename, str):
        sql_filename = "consulta.sql"
    if not isinstance(rules_sql, str) or not rules_sql.strip():
        rules_sql = sql
    if not isinstance(preview_table, str):
        preview_table = ""
    if not isinstance(preview_limit, int):
        preview_limit = 100
    preview_table = preview_table.strip()
    preview_limit = max(1, min(preview_limit, 1_000))
    if preview_table and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", preview_table):
        raise HTTPException(status_code=400, detail="El nombre de la tabla para preview no es válido.")
    definitions, families = build_catalog(rules_sql, sql_filename)
    statements = prepare_statements(sql, definitions, families)
    exports = [] if preview_table else prepare_exports(sql)
    loaded: dict[
        str,
        tuple[str, str, list[tuple[str, pd.DataFrame]], str, str | None],
    ] = {}

    for upload in files:
        filename = upload.filename or "archivo"
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Formato no permitido: {filename}")
        definition = match_definition(filename, definitions)
        family_match = match_family(filename, families) if definition is None else None
        if definition is None and family_match is None:
            expected = ", ".join(
                [item.name for item in definitions] + [item.name for item in families]
            )
            raise HTTPException(
                status_code=400,
                detail=f"El archivo '{filename}' no coincide con el catálogo. Esperados: {expected}.",
            )
        if definition is not None:
            upload_key = definition.key
            display_name = definition.name
            family_key = None
        else:
            family, period = family_match
            upload_key = f"{family.key}:p{period}"
            display_name = f"{family.prefixes[0].title()} P{period}"
            family_key = family.key

        if upload_key in loaded:
            raise HTTPException(status_code=400, detail=f"Se recibió dos veces: {display_name}.")

        payload = await upload.read(MAX_FILE_SIZE + 1)
        if len(payload) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"{filename} supera el límite de {MAX_FILE_SIZE_MB} MB.",
            )
        if definition is not None:
            target_frames = [
                (
                    target.table_name,
                    read_dataframe(
                        filename,
                        payload,
                        target.sheet_name,
                        target.range_name,
                    ),
                )
                for target in definition_targets(definition)
            ]
        else:
            target_frames = [
                (f"{family.table_name}{period}", read_dataframe(filename, payload))
            ]
        loaded[upload_key] = (
            upload_key,
            display_name,
            target_frames,
            filename,
            family_key,
        )

    missing = [item.name for item in definitions if item.required and item.key not in loaded]
    if missing:
        raise HTTPException(status_code=400, detail=f"Faltan archivos requeridos: {', '.join(missing)}.")

    for family in families:
        family_count = sum(1 for item in loaded.values() if item[4] == family.key)
        if family.required and family_count < family.min_files:
            raise HTTPException(
                status_code=400,
                detail=f"Falta {family.name}. Sube al menos {family.min_files} archivo(s) de periodo.",
            )

    connection = duckdb.connect(
        database=":memory:", config={"enable_external_access": "false"}
    )
    try:
        loaded_files = []
        for _, display_name, target_frames, filename, _ in loaded.values():
            for target_index, (table_name, dataframe) in enumerate(target_frames):
                connection.register(table_name, dataframe)
                if target_index == 0:
                    escaped_name = display_name.replace('"', '""')
                    connection.execute(
                        f'CREATE VIEW "{escaped_name}" AS SELECT * FROM "{table_name}"'
                    )
                loaded_files.append(
                    {
                        "name": display_name,
                        "filename": filename,
                        "table_name": table_name,
                        "rows": len(dataframe.index),
                        "columns": [str(column) for column in dataframe.columns],
                    }
                )

        for family in families:
            period_frames = []
            for upload_key, _, target_frames, _, family_key in loaded.values():
                if family_key != family.key:
                    continue
                period = upload_key.rsplit(":p", 1)[1]
                dataframe = target_frames[0][1]
                frame = dataframe.copy()
                frame["periodo"] = f"P{period}"
                period_frames.append(frame)
            if period_frames:
                combined = pd.concat(period_frames, ignore_index=True, sort=False)
                connection.register(family.table_name, combined)

        columns = ["estado"]
        records = [("Proceso ejecutado correctamente",)]
        truncated = False
        for statement, is_query in statements:
            cursor = connection.execute(statement)
            if not is_query:
                continue
            columns = [description[0] for description in cursor.description]
            records = cursor.fetchmany(MAX_RESULT_ROWS + 1)
            truncated = len(records) > MAX_RESULT_ROWS
            records = records[:MAX_RESULT_ROWS]
        if preview_table:
            cursor = connection.execute(
                f'SELECT * FROM "{preview_table}" LIMIT {preview_limit + 1}'
            )
            columns = [description[0] for description in cursor.description]
            records = cursor.fetchmany(preview_limit + 1)
            truncated = len(records) > preview_limit
            records = records[:preview_limit]
        output_files = []
        for export_query, filename, sheet_name in exports:
            dataframe = connection.execute(export_query).fetchdf()
            workbook = io.BytesIO()
            with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
                dataframe.to_excel(writer, index=False, sheet_name=sheet_name)
            content = workbook.getvalue()
            output_files.append(
                {
                    "filename": filename,
                    "rows": len(dataframe.index),
                    "size": len(content),
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        rows = [[json_safe(value) for value in record] for record in records]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "loaded_files": loaded_files,
            "output_files": output_files,
        }
    except duckdb.Error as error:
        raise HTTPException(status_code=400, detail=f"Error al ejecutar SQL: {error}") from error
    finally:
        connection.close()
