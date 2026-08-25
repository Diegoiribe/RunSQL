# RunSQL

Aplicación web para cargar archivos Excel/CSV con nombres estandarizados, convertirlos en tablas temporales y ejecutar una consulta SQL de solo lectura.

## Arquitectura

- **Frontend:** React, TypeScript y Vite.
- **Backend:** Python y FastAPI.
- **Motor SQL:** DuckDB en memoria.
- Los archivos se procesan durante la petición y no se almacenan en disco.

## Ejecutar en desarrollo

Requiere Python 3.9+ y Node.js 20+.

### Primera instalación

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
npm install
npm --prefix frontend install
```

### Levantar toda la aplicación

```bash
npm run dev
```

Este único comando levanta React en `http://localhost:5173` y FastAPI en `http://localhost:8000`. Vite envía automáticamente las solicitudes `/api` al backend.

También se puede iniciar todo con:

```bash
docker compose up --build
```

Para ejecutar las pruebas del backend:

```bash
cd backend
.venv/bin/python -m unittest
```

## Estandarizar archivos

El catálogo se genera automáticamente al cargar un SQL. El nombre del proceso sale del archivo SQL y las fuentes adicionales se detectan desde sus sentencias `read_xlsx`:

- `name`: nombre que verá la persona, por ejemplo `Almacenista Detalle`.
- `table_name`: nombre estable que se utiliza dentro del SQL, por ejemplo `almacenista_detalle`.
- `required`: indica si el proceso debe detenerse cuando falta el archivo.
- `aliases`: nombres alternativos aceptados sin la extensión.

Ejemplo de archivos válidos:

```text
Almacenista P1.xlsx
Almacenista P2.xlsx
Almacenista Detalle.xlsx
Datos Tienda.xlsx
Plan de Capacitación Almacenista.xlsx
```

Los archivos `P(x)` son adaptativos: puedes cargar uno, dos, tres o más periodos. Por ejemplo, `Almacenista.sql` genera `Almacenista P(x)` y `Gerente Titular.sql` genera `Gerente Titular P(x)`. Mayúsculas, minúsculas y acentos no afectan el reconocimiento.

Ejemplo de consulta:

```sql
SELECT p.*
FROM almacenista_p AS p;
```

También se puede usar el nombre visible entre comillas, como `"Almacenista P1"`.

## Reglas de seguridad actuales

- Las consultas directas aceptan `SELECT` o `WITH`.
- Los archivos SQL pueden crear tablas, vistas y macros temporales dentro de DuckDB.
- Se omiten `INSTALL`, `LOAD`, fuentes `read_xlsx` ya cubiertas por los recursos cargados y exportaciones `COPY` con rutas locales.
- Se bloquean escrituras destructivas y acceso externo desde SQL.
- En desarrollo local, cada archivo admite hasta 200 MB y la ejecución completa
  hasta 500 MB. Ambos valores se pueden configurar con `MAX_FILE_SIZE_MB` y
  `MAX_TOTAL_UPLOAD_MB`.
- La respuesta se limita a 10,000 filas para proteger el navegador.

Antes de publicarla para múltiples usuarios conviene agregar autenticación, límites por usuario, registro de ejecuciones y almacenamiento temporal aislado si se requieren descargas de resultados mayores.

## Publicación

El proyecto está preparado para publicarse completo en Vercel desde la raíz del repositorio. `api/index.py` convierte FastAPI en una función serverless: no es necesario mantener un servidor encendido y Vercel la inicia automáticamente cuando el frontend llama una ruta `/api`.

```bash
vercel
```

Frontend y backend usan el mismo dominio, por lo que no necesitas configurar `VITE_API_URL` ni CORS para Vercel.

Vercel limita el cuerpo de cada petición y respuesta de una Function a 4.5 MB.
La interfaz valida un máximo conservador de 4 MB cuando detecta ese entorno.
Los procesos con archivos grandes requieren carga directa a Vercel Blob u otro
almacenamiento, además de entregar las salidas grandes fuera de la respuesta de
la Function.
