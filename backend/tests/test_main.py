import io
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import duckdb
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.catalog import build_catalog, definition_targets
from app.firebase_publish import (
    _comment_labels,
    _publish_dashboard,
    _safe,
    _tabular_payload,
    apply_cutoff_date,
    build_dashboard_dataset,
    build_eic_dashboard_dataset,
    build_satisfaction_dashboard_dataset,
    decode_tabular_payload,
    result_table_name,
)
from app.main import execute, match_definition, match_family, normalize_quoted_identifiers, prepare_exports, prepare_statements, split_sql_script, validate_sql

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


class FakeSnapshot:
    def __init__(self, reference, data=None):
        self.reference = reference
        self.id = reference.path[-1]
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocument:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def collection(self, name):
        return FakeCollection(self.client, self.path + (name,))

    def get(self):
        return FakeSnapshot(self, self.client.documents.get(self.path))

    def set(self, payload, merge=False):
        if merge and self.path in self.client.documents:
            self.client.documents[self.path] = {
                **self.client.documents[self.path],
                **payload,
            }
        else:
            self.client.documents[self.path] = dict(payload)

    def delete(self):
        self.client.documents.pop(self.path, None)


class FakeCollection:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def document(self, name):
        return FakeDocument(self.client, self.path + (name,))

    def stream(self):
        return [
            FakeSnapshot(FakeDocument(self.client, path), payload)
            for path, payload in sorted(self.client.documents.items())
            if len(path) == len(self.path) + 1 and path[:-1] == self.path
        ]


class FakeBatch:
    def __init__(self):
        self.operations = []

    def set(self, reference, payload):
        self.operations.append(("set", reference, payload))

    def delete(self, reference):
        self.operations.append(("delete", reference, None))

    def commit(self):
        for action, reference, payload in self.operations:
            if action == "set":
                reference.set(payload)
            else:
                reference.delete()


class FakeFirestore:
    def __init__(self):
        self.documents = {}

    def collection(self, name):
        return FakeCollection(self, (name,))

    def batch(self):
        return FakeBatch()


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

    def test_multiline_excel_identifiers_are_normalized(self):
        statement = 'SELECT "Datos de Pax Reales\n(Se obtiene desde Tabla Listas)" FROM "Capacitaciones  EIC"'
        self.assertEqual(
            normalize_quoted_identifiers(statement),
            'SELECT "Datos de Pax Reales (Se obtiene desde Tabla Listas)" FROM "Capacitaciones EIC"',
        )

    def test_firestore_tabular_payload_does_not_nest_arrays(self):
        payload = _tabular_payload(
            [
                {"persona": 1, "curso": "SQL"},
                {"persona": 2, "curso": "Excel"},
            ]
        )
        self.assertEqual(payload["columns"], ["persona", "curso"])
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["values"], [1, "SQL", 2, "Excel"])
        self.assertNotIn("rows", payload)
        self.assertEqual(
            decode_tabular_payload(payload),
            [
                {"persona": 1, "curso": "SQL"},
                {"persona": 2, "curso": "Excel"},
            ],
        )

    def test_decimal_values_are_json_safe(self):
        self.assertEqual(_safe(Decimal("4004541.60755")), 4004541.60755)

    def test_eic_dataset_preserves_financial_views(self):
        connection = duckdb.connect()
        connection.execute("""
            CREATE TABLE eic_resumen_general AS
            SELECT 100.0 AS presupuesto_autorizado_mxn,
                   40.0 AS inversion_actual_mxn,
                   60.0 AS presupuesto_por_ejercer_mxn,
                   0.4 AS avance_presupuesto;
            CREATE TABLE eic_resumen_c_level AS
            SELECT 140.0 AS presupuesto_autorizado_mxn, 55.0 AS inversion_actual_mxn;
            CREATE TABLE eic_resumen_direccion AS SELECT 'Dirección A' AS direccion_nivel_2;
            CREATE TABLE eic_resumen_iniciativa AS
            SELECT 'EIC-1' AS identificador, 'Curso A' AS nombre_iniciativa,
                   'Dirección A' AS direccion_nivel_2, 'C-Level' AS direccion_c_level,
                   'Impartido' AS estatus_grupo_principal;
            CREATE TABLE eic_estatus_cotizaciones_direccion AS SELECT 1 AS registros;
            CREATE TABLE eic_estatus_capacitaciones_direccion AS SELECT 1 AS registros;
            CREATE TABLE eic_estatus_pagos_direccion AS SELECT 1 AS registros;
            CREATE TABLE eic_capacitaciones AS SELECT 'EIC-1' AS identificador;
            CREATE TABLE eic_pagos AS SELECT 'EIC-1' AS identificador;
            CREATE TABLE eic_controles AS SELECT 'PASS' AS estatus;
        """)
        dataset = build_eic_dashboard_dataset(
            connection, "eic_administrativa", "EIC Administrativa", date(2026, 8, 31)
        )
        self.assertEqual(dataset["data_kind"], "eic_administrative")
        self.assertEqual(dataset["total"], 100.0)
        self.assertEqual(dataset["completed"], 40.0)
        self.assertEqual(dataset["pending"], 60.0)
        self.assertEqual(dataset["progress_percentage"], 40.0)
        self.assertEqual(dataset["views"]["general"][0]["presupuesto_autorizado_mxn"], 100.0)
        self.assertEqual(dataset["metrics"][0]["completados"], 1)
        self.assertEqual(len(dataset["views"]["initiatives"]), 1)
        self.assertEqual(len(dataset["views"]["payments"]), 1)

    def test_dynamic_rules_ignore_case_and_accents(self):
        definitions, families = build_catalog(RULES_SQL, "ALMACÉNISTA.sql")
        self.assertEqual(
            match_definition("plan DE CAPACITACION almacenista.XLSX", definitions).table_name,
            "plan_capacitacion_almacenista",
        )
        family, period = match_family("almacenista P12.xlsx", families)
        self.assertEqual(family.table_name, "almacenista_p")
        self.assertEqual(period, 12)

    def test_gerente_zona_files_match_historical_internal_period_table(self):
        rules_sql = """
        SELECT * FROM gerente_titular_p;
        CREATE OR REPLACE VIEW resultados_capacitacion_gerente AS
        SELECT 1 AS completados, 1 AS total;
        """
        definitions, families = build_catalog(rules_sql, "Gerente Zona.sql")

        self.assertEqual(definitions, ())
        self.assertEqual(len(families), 1)
        self.assertEqual(families[0].name, "Gerente Zona P(x)")
        self.assertEqual(families[0].table_name, "gerente_titular_p")
        family, period = match_family("Gerente Zona P2.xlsx", families)
        self.assertEqual(family.table_name, "gerente_titular_p")
        self.assertEqual(period, 2)

    def test_publish_maps_gerente_zona_to_its_explicit_historical_table(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE resultados_capacitacion_gerente AS SELECT 1 AS total"
            )
            self.assertEqual(
                result_table_name(connection, "gerente_zona"),
                "resultados_capacitacion_gerente",
            )
            with self.assertRaisesRegex(ValueError, "resultados_capacitacion_cajero"):
                result_table_name(connection, "cajero")
        finally:
            connection.close()

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

    def test_fixed_workbook_process_does_not_invent_period_family(self):
        rules_sql = """
        CREATE OR REPLACE TABLE encuesta_a AS
        SELECT * FROM read_xlsx('/fuentes/Encuesta.xlsx', sheet = 'A');
        CREATE OR REPLACE TABLE encuesta_b AS
        SELECT * FROM read_xlsx('/fuentes/Encuesta.xlsx', sheet = 'B');
        SELECT * FROM encuesta_a;
        """
        definitions, families = build_catalog(rules_sql, "Encuesta de satisfaccion.sql")
        self.assertEqual(len(definitions), 1)
        self.assertEqual(families, ())

    def test_satisfaction_dataset_aggregates_isa_nps_and_quality_flag(self):
        connection = duckdb.connect(":memory:")
        connection.execute(
            """
            CREATE TABLE resultados_satisfaccion AS
            SELECT * FROM (VALUES
                (TIMESTAMP '2026-07-10 10:00:00', 'HAMI', 'HAMI', 'Ana', 'Norte', 'Puesto', 5, 5, 5, 5, 5, 10, 'Excelente curso'),
                (TIMESTAMP '2026-07-11 10:00:00', 'HAMI', 'HAMI', 'Ana', 'Norte', 'Puesto', 4, 4, 4, 4, 4, 5, 'Puede mejorar')
            ) AS t(fecha, programa, curso, instructor, region, puesto, dominio, comunicacion, interes, participacion, resolucion, recomendacion, comentario)
            """
        )
        dataset = build_satisfaction_dashboard_dataset(
            connection, "encuesta_de_satisfaccion", "Encuesta de satisfacción", date(2026, 7, 31)
        )
        self.assertEqual(dataset["data_kind"], "satisfaction")
        self.assertEqual(dataset["response_count"], 2)
        self.assertEqual(dataset["isa"], 90.0)
        self.assertEqual(dataset["nps"], 0.0)
        self.assertEqual(dataset["nps_scale_status"], "conventional_0_10")
        self.assertEqual(dataset["score_five_responses"], 1)
        self.assertEqual(
            len([row for row in dataset["detail_rows"] if row["record_type"] == "comment"]),
            2,
        )
        self.assertTrue(
            any(row["record_type"] == "theme" for row in dataset["detail_rows"])
        )
        connection.close()

    def test_comment_sentiment_does_not_confuse_improvement_with_an_opportunity(self):
        self.assertEqual(
            _comment_labels("Fue muy amena y participativa. Ideal para mejorar habilidades.")[1],
            "positive",
        )
        self.assertEqual(
            _comment_labels("Excelente, realizó dinámicas y siempre estuvo atento; respondió de la mejor forma.")[1],
            "positive",
        )
        self.assertEqual(
            _comment_labels("Mejorar el equipo: algunas partes ya no funcionan.")[1],
            "negative",
        )
        self.assertEqual(
            _comment_labels("Excelente curso, pero deben mejorar los materiales para práctica.")[1],
            "negative",
        )
        self.assertEqual(
            _comment_labels("Dominio de los temas, con un carisma impresionante y conecta muy rápido con las personas.")[1],
            "positive",
        )
        self.assertEqual(
            _comment_labels("El curso fue muy práctico; así deberían ser todos los cursos, con gente dinámica.")[1],
            "positive",
        )
        self.assertEqual(
            _comment_labels("El instructor no fue claro y fue difícil de seguir.")[1],
            "negative",
        )
        self.assertEqual(
            _comment_labels("El curso fue bueno, pero el equipo no funciona.")[1],
            "negative",
        )

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

    def test_cutoff_date_replaces_parameter_and_validation_date(self):
        sql = """
        CREATE OR REPLACE TABLE parametros_asesor AS
        SELECT DATE '2026-07-30' AS fecha_corte;
        SELECT * FROM parametros_asesor
        WHERE fecha_corte <> DATE '2026-07-30';
        """
        updated = apply_cutoff_date(sql, "2026-08-31")
        self.assertNotIn("2026-07-30", updated)
        self.assertEqual(updated.count("2026-08-31"), 2)

    def test_dashboard_dataset_supports_filters_and_pending_detail(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE resultados_capacitacion_asesor AS
                SELECT * FROM (VALUES
                    (1, 'Ana', 'Gerente Ventas', '1', 'Curso A', 'No', 1, 1, '10'),
                    (2, 'Luis', 'Gerente Muebles', '2', 'Curso A', 'No', 0, 1, '20'),
                    (2, 'Luis', 'Gerente Muebles', '2', 'Curso B', 'No', 0, 1, '20'),
                    (3, 'Eva', 'Gerente Ventas', '1', 'Iniciativa', 'Si', 0, 1, '30')
                ) AS source(
                    numero_persona, nombre, nombre_puesto, region, curso,
                    iniciativa, completados, total, tienda
                )
                """
            )
            dataset = build_dashboard_dataset(
                connection,
                "asesor",
                "Asesor",
                date(2026, 7, 30),
            )
        finally:
            connection.close()
        self.assertEqual(dataset["period"], "2026-07")
        self.assertEqual(dataset["total"], 3)
        self.assertEqual(dataset["completed"], 1)
        self.assertEqual(dataset["pending"], 2)
        self.assertEqual(dataset["progress_percentage"], 33.33)
        self.assertEqual(len(dataset["pending_rows"]), 2)
        self.assertEqual(
            dataset["positions"], ["Gerente Muebles", "Gerente Ventas"]
        )
        self.assertEqual(len(dataset["views"]["positions"]), 2)
        self.assertEqual(
            sum(row["total"] for row in dataset["views"]["regions"]), 3
        )
        self.assertEqual(dataset["views"]["regions"][1]["pendientes"], 2)
        self.assertEqual(dataset["views"]["courses"][0]["avance"], 50.0)

    def test_same_category_and_cutoff_replaces_previous_publication(self):
        client = FakeFirestore()
        first = {
            "period": "2026-07",
            "category": "almacenista",
            "category_label": "Almacenista",
            "cutoff_date": "2026-07-30",
            "year": 2026,
            "month": 7,
            "total": 2,
            "completed": 1,
            "pending": 1,
            "progress_percentage": 50.0,
            "pending_percentage": 50.0,
            "positions": ["Almacenista"],
            "regions": ["Norte"],
            "courses": ["Curso A"],
            "metrics": [
                {
                    "puesto": "Almacenista",
                    "region": "Norte",
                    "curso": "Curso A",
                    "total": 2,
                    "completados": 1,
                    "pendientes": 1,
                    "avance": 50.0,
                }
            ],
            "views": {
                "positions": [],
                "regions": [],
                "courses": [],
                "course_regions": [],
            },
            "pending_rows": [
                {
                    "numero_persona": "1",
                    "nombre": "Ana",
                    "puesto": "Almacenista",
                    "region": "Norte",
                    "tienda": "10",
                    "curso": "Curso A",
                }
            ],
            "detail_rows": [
                {
                    "record_type": "comment",
                    "comentario": "Excelente curso",
                }
            ],
        }
        updated = {
            **first,
            "total": 3,
            "completed": 3,
            "pending": 0,
            "progress_percentage": 100.0,
            "pending_percentage": 0.0,
            "metrics": [
                {
                    **first["metrics"][0],
                    "total": 3,
                    "completados": 3,
                    "pendientes": 0,
                    "avance": 100.0,
                }
            ],
            "pending_rows": [],
        }

        with patch("app.firebase_publish._firebase_client", return_value=client):
            created = _publish_dashboard(first)
            replaced = _publish_dashboard(updated)

        self.assertFalse(created["replaced_existing"])
        self.assertEqual(created["publication_revision"], 1)
        self.assertTrue(replaced["replaced_existing"])
        self.assertTrue(replaced["same_cutoff_replacement"])
        self.assertEqual(replaced["publication_revision"], 2)

        category = (
            client.collection("periods")
            .document("2026-07")
            .collection("categories")
            .document("almacenista")
        )
        metadata = category.get().to_dict()
        self.assertEqual(metadata["total"], 3)
        self.assertEqual(metadata["pending"], 0)
        self.assertEqual(metadata["publication_action"], "replaced")
        self.assertEqual(metadata["publication_revision"], 2)
        self.assertEqual(metadata["detail_chunks"], 1)
        self.assertEqual(list(category.collection("pending_chunks").stream()), [])

        cube = category.collection("view_chunks").document("cube_00000").get()
        rows = decode_tabular_payload(cube.to_dict())
        self.assertEqual(rows[0]["total"], 3)
        self.assertEqual(rows[0]["completados"], 3)

        details = category.collection("detail_chunks").document("detail_00000").get()
        self.assertEqual(
            decode_tabular_payload(details.to_dict()),
            [{"record_type": "comment", "comentario": "Excelente curso"}],
        )

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

    async def test_start_date_publishes_dashboard_instead_of_excel(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n1,SQL\n"), filename="Almacenista P1.csv"),
            *self.required_files(),
        ]
        sql = """
        CREATE OR REPLACE TABLE parametros_almacenista AS
        SELECT DATE '2026-07-30' AS fecha_corte;
        CREATE OR REPLACE TABLE resultados_capacitacion_almacenista AS
        SELECT
            1 AS numero_persona, 'Ana' AS nombre,
            'Almacenista' AS nombre_puesto, '1' AS region,
            'Curso A' AS curso, 'No' AS iniciativa,
            0 AS completados, 1 AS total, '10' AS tienda;
        SELECT fecha_corte FROM parametros_almacenista;
        COPY (SELECT * FROM resultados_capacitacion_almacenista)
        TO '/equipo/resultado.xlsx';
        """
        publication = {
            "project_id": "capacitaciones-api",
            "period": "2026-08",
            "category": "almacenista",
            "metric_rows": 1,
            "pending_rows": 1,
            "view_documents": 5,
            "detail_documents": 1,
            "documents_written": 4,
        }
        with patch("app.main.publish_dashboard", return_value=publication) as publish:
            result = await execute(
                sql,
                files,
                "Almacenista.sql",
                RULES_SQL,
                "",
                100,
                "2026-08-31",
                True,
            )
        dataset = publish.call_args.args[0]
        self.assertEqual(dataset["cutoff_date"], "2026-08-31")
        self.assertEqual(dataset["pending"], 1)
        self.assertEqual(result["output_files"], [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["firebase_publish"], publication)

    async def test_publication_name_and_collection_do_not_change_source_table(self):
        files = [
            UploadFile(io.BytesIO(b"persona,curso\n1,SQL\n"), filename="Almacenista P1.csv"),
            *self.required_files(),
        ]
        sql = """
        CREATE OR REPLACE TABLE parametros_almacenista AS
        SELECT DATE '2026-07-30' AS fecha_corte;
        CREATE OR REPLACE TABLE resultados_capacitacion_almacenista AS
        SELECT
            1 AS numero_persona, 'Ana' AS nombre,
            'Almacenista' AS nombre_puesto, '1' AS region,
            'Curso A' AS curso, 'No' AS iniciativa,
            1 AS completados, 1 AS total, '10' AS tienda;
        """
        publication = {
            "project_id": "capacitaciones-api",
            "period": "2026-08",
            "category": "eic_presupuesto",
            "metric_rows": 1,
            "pending_rows": 0,
        }
        with patch("app.main.publish_dashboard", return_value=publication) as publish:
            await execute(
                sql=sql,
                files=files,
                sql_filename="Almacenista.sql",
                rules_sql=RULES_SQL,
                cutoff_date="2026-08-31",
                publish_to_firebase=True,
                publication_name="EIC Presupuesto",
                collection_link="Administración EIC",
                report_type="c",
            )

        dataset = publish.call_args.args[0]
        self.assertEqual(dataset["category"], "eic_presupuesto")
        self.assertEqual(dataset["category_label"], "EIC Presupuesto")
        self.assertEqual(dataset["collection_key"], "administracion_eic")
        self.assertEqual(dataset["collection_label"], "Administración EIC")
        self.assertEqual(dataset["report_type"], "c")

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
