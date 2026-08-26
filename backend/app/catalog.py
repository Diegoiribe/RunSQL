from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileTarget:
    table_name: str
    sheet_name: str | int = 0
    range_name: str = ""


@dataclass(frozen=True)
class FileDefinition:
    key: str
    name: str
    table_name: str
    required: bool
    aliases: tuple[str, ...] = ()
    description: str = ""
    sheet_name: str | int = 0
    range_name: str = ""
    targets: tuple[FileTarget, ...] = ()

    def public_dict(self) -> dict:
        value = asdict(self)
        value["aliases"] = list(self.aliases)
        return value


@dataclass(frozen=True)
class FileFamily:
    key: str
    name: str
    table_name: str
    required: bool
    min_files: int
    prefixes: tuple[str, ...]
    description: str = ""

    def public_dict(self) -> dict:
        value = asdict(self)
        value["prefixes"] = list(self.prefixes)
        return value


# Este es el único lugar que necesitas editar cuando cambie la lista de archivos.
FILE_CATALOG = (
    FileDefinition(
        key="almacenista_detalle",
        name="Almacenista Detalle",
        table_name="almacenista_detalle",
        required=True,
        aliases=(
            "almacenista_detalle",
            "detalle almacenista",
            "detalle colaborador almacenista",
            "bodeguita detalle colaborador",
            "bodeguita detalle colaborador 10",
        ),
        description="Detalle de colaboradores de Almacenista y Auxiliar de Piso.",
        sheet_name="Sheet1",
    ),
    FileDefinition(
        key="datos_tiendas_almacenista",
        name="Datos Tienda",
        table_name="datos_tiendas_almacenista",
        required=True,
        aliases=(
            "datos tienda",
            "datos tiendas",
            "datos_tiendas_almacenista",
            "7 datos tiendas julio 2026",
        ),
        description="Catálogo de tiendas y Gerente de Zona.",
        sheet_name="Hoja2",
    ),
    FileDefinition(
        key="plan_capacitacion_almacenista",
        name="Plan de Capacitación Almacenista",
        table_name="plan_capacitacion_almacenista",
        required=True,
        aliases=(
            "plan capacitacion almacenista",
            "plan de capacitacion almacenista",
            "plan_capacitacion_almacenista",
        ),
        description="Cursos, temporalidad e iniciativas del plan.",
        sheet_name="Hoja1",
    ),
)

# Una familia admite P1, P2, P3... sin establecer un máximo.
# Todos sus archivos se combinan en la tabla indicada por table_name.
FILE_FAMILIES = (
    FileFamily(
        key="almacenista_periodos",
        name="Almacenista P(x)",
        table_name="almacenista_p",
        required=True,
        min_files=1,
        prefixes=("almacenista", "almacenista y auxiliar de piso", "auxiliar de piso"),
        description="Uno o más periodos: Almacenista P1, Almacenista P2, Almacenista P3, etc.",
    ),
)


SOURCE_PATTERN = re.compile(
    r"create\s+or\s+replace\s+table\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s+as\s+"
    r"select\s+\*\s+from\s+read_xlsx\s*\(\s*'([^']+)'(.*?)\)\s*;",
    flags=re.IGNORECASE | re.DOTALL,
)


def definition_targets(definition: FileDefinition) -> tuple[FileTarget, ...]:
    if definition.targets:
        return definition.targets
    return (
        FileTarget(
            table_name=definition.table_name,
            sheet_name=definition.sheet_name,
            range_name=definition.range_name,
        ),
    )


def normalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def process_name(sql_filename: str) -> str:
    stem = Path(sql_filename or "Proceso").stem.strip()
    stem = re.sub(r"\s*ejemplo$", "", stem, flags=re.IGNORECASE).strip()
    return stem or "Proceso"


def identifier_label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_") if part)


def source_display_name(process: str, table_name: str) -> str:
    normalized_table = normalize_label(table_name)
    normalized_process = normalize_label(process)
    tokens = normalized_table.split()
    process_tokens = set(normalized_process.split())

    if "datos" in tokens and any(token.startswith("tienda") for token in tokens):
        return "Datos Tienda"
    if "plan" in tokens and "capacitacion" in tokens:
        return f"Plan de Capacitación {process}"
    if "planta" in tokens and "posiciones" in tokens:
        return f"Planta de {process} por Posiciones"
    if "planta" in tokens and any(token.startswith("centro") for token in tokens):
        return f"Planta de {process} por Centro"
    if "detalle" in tokens:
        qualifiers = [token for token in tokens if token not in process_tokens and token != "detalle"]
        suffix = f" {identifier_label('_'.join(qualifiers))}" if qualifiers else ""
        return f"{process}{suffix} Detalle"

    remainder = [
        token
        for token in tokens
        if token not in process_tokens and token not in {"fuente", "source"}
    ]
    return f"{process} {identifier_label('_'.join(remainder))}".strip()


def build_catalog(sql: str, sql_filename: str) -> tuple[tuple[FileDefinition, ...], tuple[FileFamily, ...]]:
    process = process_name(sql_filename)
    process_key = re.sub(r"\s+", "_", normalize_label(process)) or "proceso"
    source_matches = list(SOURCE_PATTERN.finditer(sql))
    period_sources: dict[str, list[tuple[str, str]]] = {}
    definitions: list[FileDefinition] = []
    source_groups: dict[str, list[tuple[str, str, str]]] = {}

    for match in source_matches:
        table_name, source_path, options = match.groups()
        original_stem = Path(source_path).stem
        period_match = re.fullmatch(r"(.+?)_p(\d+)", table_name, flags=re.IGNORECASE)
        if period_match:
            family_table, period = period_match.groups()
            period_sources.setdefault(f"{family_table}_p", []).append((period, original_stem))
            continue

        source_groups.setdefault(normalize_label(source_path), []).append(
            (table_name, source_path, options)
        )

    for grouped_sources in source_groups.values():
        first_table, first_path, _ = grouped_sources[0]
        original_stem = Path(first_path).stem
        targets: list[FileTarget] = []
        aliases = {original_stem}
        for table_name, _, options in grouped_sources:
            sheet_match = re.search(r"\bsheet\s*=\s*'([^']+)'", options, flags=re.IGNORECASE)
            range_match = re.search(r"\brange\s*=\s*'([^']+)'", options, flags=re.IGNORECASE)
            target_display_name = source_display_name(process, table_name)
            aliases.update(
                {
                    table_name,
                    target_display_name,
                    f"{process} {identifier_label(table_name)}",
                }
            )
            targets.append(
                FileTarget(
                    table_name=table_name.lower(),
                    sheet_name=sheet_match.group(1) if sheet_match else 0,
                    range_name=range_match.group(1) if range_match else "",
                )
            )

        display_name = (
            original_stem
            if len(targets) > 1
            else source_display_name(process, first_table)
        )
        primary = targets[0]
        definitions.append(
            FileDefinition(
                key=f"{process_key}:{first_table.lower()}",
                name=display_name,
                table_name=primary.table_name,
                required=True,
                aliases=tuple(sorted(aliases)),
                description="",
                sheet_name=primary.sheet_name,
                range_name=primary.range_name,
                targets=tuple(targets),
            )
        )

    families: list[FileFamily] = []
    for index, (table_name, sources) in enumerate(period_sources.items()):
        qualifier = table_name.removesuffix("_p")
        qualifier_tokens = [
            token for token in normalize_label(qualifier).split()
            if token not in set(normalize_label(process).split())
        ]
        qualifier_label = f" {identifier_label('_'.join(qualifier_tokens))}" if len(period_sources) > 1 and qualifier_tokens else ""
        family_name = f"{process}{qualifier_label} P(x)"
        prefixes = {f"{process}{qualifier_label}", qualifier.replace("_", " ")}
        if len(period_sources) == 1:
            prefixes.add(process)
        for period, original_stem in sources:
            prefixes.add(re.sub(rf"\s*p\s*{re.escape(period)}$", "", original_stem, flags=re.IGNORECASE).strip())
        families.append(
            FileFamily(
                key=f"{process_key}:periodos:{index}",
                name=family_name,
                table_name=table_name.lower(),
                required=True,
                min_files=1,
                prefixes=tuple(sorted(prefixes)),
                description=f"Periodos adaptativos de {process}.",
            )
        )

    if not families and sql_filename and Path(sql_filename).stem.lower() != "consulta":
        combined_matches = re.findall(r"\b([a-z][a-z0-9_]*_p)\b", sql, flags=re.IGNORECASE)
        process_root = process_key.split("_")[0]
        preferred_matches = [
            value.lower() for value in combined_matches if process_root in value.lower()
        ]
        inferred_tables = list(dict.fromkeys(preferred_matches))
        if not inferred_tables:
            inferred_tables = [f"{process_key}_p"]

        for index, table_name in enumerate(inferred_tables):
            qualifier = table_name.removesuffix("_p")
            qualifier_tokens = [
                token
                for token in normalize_label(qualifier).split()
                if token not in set(normalize_label(process).split())
            ]
            qualifier_label = (
                f" {identifier_label('_'.join(qualifier_tokens))}"
                if len(inferred_tables) > 1 and qualifier_tokens
                else ""
            )
            family_name = f"{process}{qualifier_label} P(x)"
            family_prefix = f"{process}{qualifier_label}"
            families.append(
                FileFamily(
                    key=f"{process_key}:periodos:{index}",
                    name=family_name,
                    table_name=table_name,
                    required=True,
                    min_files=1,
                    prefixes=(family_prefix,),
                    description=f"Periodos adaptativos de {process}{qualifier_label}.",
                )
            )

    return tuple(definitions), tuple(families)
