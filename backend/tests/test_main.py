import io
import unittest

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.catalog import build_catalog, definition_targets
from app.main import execute, match_definition, match_family, prepare_exports, prepare_statements, split_sql_script, validate_sql

RULES_SQL = """
-- La aplicación registra almacenista_p.
CREATE OR REPLACE TABLE almacenista_detalle AS
SELECT * FROM read_xlsx('/fuentes/Almacenista Detalle.xlsx', sheet = 'Sheet1');
CREATE OR REPLACE TABLE datos_tiendas_almacenista AS
SELECT * FROM read_xlsx('/fuentes/Datos Tienda.xlsx', sheet = 'Hoja2');
CREATE OR REPLACE TABLE plan_capacitacion_almacenista AS
SELECT * FROM read_xlsx('/fuentes/Plan de Capacitacion Almacenista.xlsx', sheet = 'Hoja1');
SELECT * FROM almacenista_p;
"""


class RunSqlTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def required_files():
        return [
            UploadFile(io.BytesIO(b"id,valor\n1,ok\n"), filename="Almacenista Detalle.csv"),
            UploadFile(io.BytesIO(b"id,zona\n1,Norte\n"), filename="Datos Tienda.csv"),
            UploadFile(
                io.BytesIO(b"Curso,Curso LMS,Temporalidad,Iniciativa\nSQL,SQL,1,No\n"),
                filename="Plan de Capacitacion Almacenista.csv",
            ),
        ]

    def test_file_names_are_normalized(self):
        family, period = match_family("Almacenista y Auxiliar de Piso P3.xlsx")
        self.assertEqual(family.key, "almacenista_periodos")
        self.assertEqual(period, 3)
        self.assertEqual(match_definition("Plan de Capacitacion Almacenista.CSV").key, "plan_capacitacion_almacenista")
        self.assertEqual(match_definition("7 Datos Tiendas Julio 2026.xlsx").key, "datos_tiendas_almacenista")
        self.assertEqual(match_definition("Bodeguita _ Detalle Colaborador (10).xlsx").key, "almacenista_detalle")
        self.assertIsNone(match_definition("archivo desconocido.xlsx"))

    def test_dynamic_rules_ignore_case_and_accents(self):
        definitions, families = build_catalog(RULES_SQL, "ALMACÉNISTA.sql")
        self.assertEqual(
            match_definition("plan DE CAPACITACION almacenista.XLSX", definitions).table_name,
            "plan_capacitacion_almacenista",
        )
        family, period = match_family("almacenista P12.xlsx", families)
        self.assertEqual(family.table_name, "almacenista_p")
        self.assertEqual(period, 12)

    def test_catalog_supports_multiple_adaptive_period_families(self):
        rules_sql = """
        CREATE OR REPLACE TABLE cat_detalle_operacion AS
        SELECT * FROM read_xlsx('/fuentes/CAT Operacion Detalle.xlsx', sheet = 'Hoja1');
        CREATE OR REPLACE TABLE cat_detalle_gerencial AS
        SELECT * FROM read_xlsx('/fuentes/CAT Gerencial Detalle.xlsx', sheet = 'Hoja1');
        SELECT * FROM cat_operacion_p;
        SELECT * FROM cat_gerencial_p;
        """
        definitions, families = build_catalog(rules_sql, "CAT.sql")
        self.assertEqual(
            [item.name for item in definitions],
            ["CAT Operacion Detalle", "CAT Gerencial Detalle"],
        )
        self.assertEqual(
            [item.name for item in families],
            ["CAT Operacion P(x)", "CAT Gerencial P(x)"],
        )
        operation, period = match_family("CAT OPERACIÓN P3.xlsx", families)
        self.assertEqual(operation.table_name, "cat_operacion_p")
        self.assertEqual(period, 3)
        management, period = match_family("cat gerencial p12.xlsx", families)
        self.assertEqual(management.table_name, "cat_gerencial_p")
        self.assertEqual(period, 12)

    def test_catalog_groups_multiple_sheets_from_one_workbook(self):
        rules_sql = """
        CREATE OR REPLACE TABLE plan_staff_fuente AS
        SELECT * FROM read_xlsx('/fuentes/Concentrado Staff.xlsx', sheet = 'Plan capacitacion');
        CREATE OR REPLACE TABLE historico_staff_fuente AS
        SELECT * FROM read_xlsx('/fuentes/Concentrado Staff.xlsx', sheet = 'Historico Staff');
        CREATE OR REPLACE TABLE demograficos_staff_fuente AS
        SELECT * FROM read_xlsx('/fuentes/STAFF Demograficos.xlsx', sheet = 'Sheet1', range = 'A2:Z');
        SELECT * FROM staff_p;
        """
        definitions, families = build_catalog(rules_sql, "STAFF.sql")
        self.assertEqual(len(definitions), 2)
        concentrated = next(item for item in definitions if item.name == "Concentrado Staff")
        self.assertEqual(
            [(target.table_name, target.sheet_name) for target in definition_targets(concentrated)],
            [
                ("plan_staff_fuente", "Plan capacitacion"),
                ("historico_staff_fuente", "Historico Staff"),
            ],
        )
        demographics = next(item for item in definitions if item.table_name == "demograficos_staff_fuente")
        self.assertEqual(definition_targets(demographics)[0].range_name, "A2:Z")
        self.assertEqual(families[0].table_name, "staff_p")

    def test_staff_catalog_accepts_confidential_demographics_and_periods(self):
        rules_sql = """
        CREATE OR REPLACE TABLE demograficos_staff_fuente AS
        SELECT * FROM read_xlsx(
            '/fuentes/EIC CONFIDENCIAL - Planta Activa Demograficos GC.xlsx',
            sheet = 'Sheet1', range = 'A2:Z'
        );
        CREATE OR REPLACE TABLE jerarquia_staff_fuente AS
        SELECT * FROM read_xlsx(
            '/fuentes/EIC CONFIDENCIAL - Jerarquia Planta Activa Demograficos GC (7).xlsx',
            sheet = 'Sheet1', range = 'A3:P'
        );
        SELECT * FROM staff_p;
        """
        definitions, families = build_catalog(rules_sql, "STAFF.sql")
        self.assertEqual(
            [definition.name for definition in definitions],
            ["STAFF Demograficos", "STAFF Jerarquia"],
        )
        demographics = match_definition(
            "eic confidencial - planta activa demograficos gc.XLSX", definitions
        )
        self.assertEqual(demographics.table_name, "demograficos_staff_fuente")
        family, period = match_family("staff p12.xlsx", families)
        self.assertEqual(family.table_name, "staff_p")
        self.assertEqual(period, 12)

    def test_sql_guard_blocks_file_access(self):
        with self.assertRaises(HTTPException):
            validate_sql("SELECT * FROM read_csv('/etc/passwd')")

    def test_sql_guard_blocks_multiple_statements(self):
        with self.assertRaises(HTTPException):
            validate_sql("SELECT 1; SELECT 2")

    def test_web_script_ignores_local_sources_and_exports(self):
        sql = """
        -- Un punto y coma aquí; no debe cortar el comentario.
        INSTALL excel;
        CREATE OR REPLACE TABLE almacenista_detalle AS
        SELECT * FROM read_xlsx('/ruta/local.xlsx');
        CREATE OR REPLACE VIEW resumen AS SELECT * FROM almacenista_p;
        SELECT COUNT(*) AS total FROM resumen;
        COPY (SELECT * FROM resumen) TO '/ruta/salida.xlsx';
        """
        self.assertEqual(len(split_sql_script(sql)), 5)
        prepared = prepare_statements(sql)
        self.assertEqual(len(prepared), 2)
        self.assertTrue(prepared[-1][1])
        exports = prepare_exports(sql)
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0][1], "salida.xlsx")

    async def test_execute_returns_xlsx_exports(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n1,SQL\n"), filename="Almacenista P1.csv"),
            *self.required_files(),
        ]
        result = await execute(
            """
            CREATE OR REPLACE TABLE resumen AS SELECT * FROM almacenista_p;
            COPY (SELECT persona, curso FROM resumen ORDER BY persona)
            TO '/equipo/Reporte Almacenista.xlsx'
            WITH (FORMAT xlsx, HEADER true, SHEET 'Reporte');
            """,
            files,
            "Almacenista.sql",
            RULES_SQL,
        )
        self.assertEqual(result["output_files"][0]["filename"], "Reporte Almacenista.xlsx")
        self.assertEqual(result["output_files"][0]["rows"], 1)
        self.assertTrue(result["output_files"][0]["content_base64"].startswith("UEs"))

    async def test_preview_generated_table(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n2,React\n1,SQL\n"), filename="Almacenista P1.csv"),
            *self.required_files(),
        ]
        script = """
        CREATE OR REPLACE TABLE tabla_generada AS
        SELECT persona, curso FROM almacenista_p ORDER BY persona;
        """
        result = await execute(
            script,
            files,
            "Almacenista.sql",
            RULES_SQL,
            "tabla_generada",
            1,
        )
        self.assertEqual(result["columns"], ["persona", "curso"])
        self.assertEqual(result["rows"], [[1, "SQL"]])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["output_files"], [])

    async def test_execute_joins_uploaded_files(self):
        files = [
            UploadFile(
                io.BytesIO(b"numero_empleado,nombre\n1,Ana\n2,Luis\n"),
                filename="Almacenista P1.csv",
            ),
            *self.required_files(),
        ]
        result = await execute(
            "SELECT nombre FROM almacenista_p1 ORDER BY nombre",
            files,
            "Almacenista.sql",
            RULES_SQL,
        )
        self.assertEqual(result["rows"], [["Ana"], ["Luis"]])

    async def test_period_count_is_adaptive(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n1,SQL\n"), filename="Almacenista P1.csv"),
            UploadFile(io.BytesIO(b"persona,curso\n2,React\n"), filename="Almacenista P2.csv"),
            UploadFile(io.BytesIO(b"persona,curso\n3,Python\n"), filename="Almacenista P3.csv"),
            *self.required_files(),
        ]
        result = await execute(
            "SELECT persona, curso, periodo FROM almacenista_p ORDER BY persona",
            files,
            "Almacenista.sql",
            RULES_SQL,
        )
        self.assertEqual(
            result["rows"],
            [[1, "SQL", "P1"], [2, "React", "P2"], [3, "Python", "P3"]],
        )

    async def test_safe_multi_statement_script_runs(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n1,SQL\n2,React\n"), filename="Almacenista P1.csv"),
            *self.required_files(),
        ]
        result = await execute(
            """
            CREATE OR REPLACE TABLE resumen AS SELECT * FROM almacenista_p;
            SELECT COUNT(*) AS total FROM resumen;
            """,
            files,
            "Almacenista.sql",
            RULES_SQL,
        )
        self.assertEqual(result["rows"], [[2]])


if __name__ == "__main__":
    unittest.main()
