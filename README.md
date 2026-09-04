# RunSQL

Aplicación web para cargar archivos Excel/CSV con nombres estandarizados, convertirlos en tablas temporales y ejecutar una consulta SQL de solo lectura.

Para operar la aplicación paso a paso consulta el [Manual de usuario](MANUAL_DE_USUARIO.md).

## Arquitectura

- **Frontend:** React, TypeScript y Vite.
- **Backend:** Python y FastAPI.
- **Motor SQL:** DuckDB en memoria.
- Los archivos se procesan durante la petición y no se almacenan en disco.

## Ejecutar localmente en macOS y Windows

Requiere Python 3.9 a 3.13 y Node.js 20 o posterior. Se recomienda Python 3.12.

La aplicación utiliza los mismos archivos y la misma interfaz en ambos sistemas.
El iniciador detecta automáticamente la plataforma, crea el entorno de Python
correcto e inicia React y FastAPI al mismo tiempo.

### Opción rápida en macOS

Abre `start-mac.command` con doble clic.

Si macOS bloquea el archivo la primera vez, desde una terminal ejecuta:

```bash
chmod +x start-mac.command
./start-mac.command
```

### Opción rápida en Windows

Abre `start-windows.bat` con doble clic.

No necesitas activar manualmente el entorno virtual. Windows utiliza
`backend/.venv-windows` y macOS utiliza `backend/.venv`, por lo que una copia del
proyecto puede moverse entre ambas computadoras sin mezclar ejecutables.

### Instalación desde terminal

Los siguientes comandos son iguales en PowerShell, Símbolo del sistema y la
Terminal de macOS:

```bash
npm install
npm run setup
npm run dev
```

Después de la primera preparación, normalmente basta con:

```bash
npm run dev
```

`npm run dev` levanta React en `http://localhost:5173` y FastAPI en
`http://localhost:8000`. Vite envía automáticamente las solicitudes `/api` al
backend.

### Requisitos en la computadora del trabajo

- Node.js 20 o posterior.
- Python 3.9 a 3.13; se recomienda Python 3.12.
- Permiso para ejecutar programas desde la carpeta del proyecto.
- Acceso a internet únicamente durante la primera instalación de dependencias.

No requiere permisos de administrador si Node.js y Python ya están instalados
para el usuario. En Windows, cuando hace falta, `npm run setup` descarga el
runtime oficial de Microsoft Visual C++ y lo guarda únicamente en
`backend/.runtime-windows`.
No instala componentes en Windows, no modifica el registro y no afecta a otros
usuarios.

### Reparar el entorno de Python en Windows

Si el frontend abre pero el backend informa que una dependencia no se puede
importar, confirma primero que Python 3.12 esté disponible:

```powershell
py -3.12 --version
```

Después reconstruye únicamente el entorno generado de Windows:

```powershell
Remove-Item -Recurse -Force .\backend\.venv-windows
npm.cmd run setup
npm.cmd run dev
```

La carpeta del proyecto puede contener espacios y acentos. El iniciador configura
la consola y Python en UTF-8 para conservar correctamente rutas como
`Documents\Programación\RunSQL`.

Si el error menciona `DLL load failed while importing duckdb`, no ejecutes el
instalador de Visual C++ que solicita administrador. Reconstruye la preparación
local del proyecto:

```powershell
Remove-Item -Recurse -Force .\backend\.runtime-windows -ErrorAction SilentlyContinue
npm.cmd run setup
npm.cmd run dev
```

El paquete se descarga desde `download.microsoft.com`, se verifica por SHA-256 y
solo se extraen sus DLL dentro del proyecto. Si la política corporativa bloquea
la descarga, puedes preparar esa carpeta en otra computadora Windows y copiarla
junto con el proyecto.

También se puede iniciar todo con:

```bash
docker compose up --build
```

Para ejecutar las pruebas del backend:

```bash
npm run test:backend
```

## Publicar resultados en Firebase

La ejecución mensual ya no necesita generar los Excel de salida. El comando:

```text
start --d(2026-07-30)
```

usa esa fecha como corte del SQL, ejecuta el proceso cargado y publica en Cloud
Firestore. El nombre del SQL determina la categoría (`Asesor.sql` publica
`asesor`, `Gerente Zona.sql` publica `gerente_zona`, etcétera).

El nombre público y la colección se pueden indicar sin renombrar el SQL:

```text
start --d(2026-09-02) --t(e) --n(Dirección de Estrategia y crecimiento) --l(Planes de capacitación)
```

- `--n(nombre)` define la identidad y el nombre visible. Si el reporte ya existe,
  lo reemplaza conservando el mismo nombre; el segundo parámetro no es obligatorio.
- `--n(nombre actual, nombre nuevo)` reemplaza la categoría existente identificada
  por el nombre actual y, después de publicar, la muestra con el nombre nuevo.
- `--l(colección)` lo vincula mediante metadatos con una colección existente.
- `--t(tipo)` selecciona la estructura: `c` para capacitación, `s` para encuesta
  de satisfacción y `e` para estatus administrativo EIC.
- Las opciones pueden escribirse en cualquier orden. `--d(...)` continúa siendo
  obligatorio; `--t(...)`, `--n(...)` y `--l(...)` son opcionales para conservar
  compatibilidad con los procesos existentes.

El vínculo no reemplaza la colección: cada reporte conserva su propio documento
y DataStore puede agrupar todos los que compartan `collection_key`.

Para los reportes EIC se carga una sola vez `EIC Maestro.sql`. Después puede
recibir cualquier libro llamado `EIC - Restringida - Vista Cliente <C-Level>.xlsx`.
RunSQL detecta encabezados desplazados y normaliza alias conocidos, mientras
`--n(...)` determina qué capítulo C-Level se reemplaza o se crea.

En **Planes de capacitación** y **Encuesta de satisfacción**, un solo nombre
permite actualizar el reporte con la misma identidad. En **Tienda**, un reemplazo
requiere que coincidan el nombre y la fecha exacta de corte; RunSQL detiene la
publicación si intentara sobrescribir el mismo periodo con otra fecha.

La especificación completa del reporte administrativo está en
[`DIRECCION_ADMINISTRACION_GC.md`](DIRECCION_ADMINISTRACION_GC.md).

La documentación funcional y técnica de satisfacción, incluida la clasificación
de comentarios, oportunidades y propuestas, está en
[`ENCUESTA_SATISFACCION.md`](ENCUESTA_SATISFACCION.md).

La configuración web de Firebase identifica el proyecto, pero no concede al
backend permiso para escribir. Para publicar de forma segura:

1. En Firebase Console abre **Configuración del proyecto > Cuentas de servicio**.
2. Genera una clave privada nueva y guárdala fuera del repositorio.
3. Copia `.env.example` como `.env`.
4. Coloca en `FIREBASE_SERVICE_ACCOUNT` la ruta absoluta al JSON.

Ejemplo de Windows:

```dotenv
FIREBASE_PROJECT_ID=capacitaciones-api
FIREBASE_SERVICE_ACCOUNT=C:\Users\diego.iribe\Documents\firebase-service-account.json
```

La clave está excluida de Git. No debe pegarse en el frontend ni compartirse.

### Estructura de datos

```text
periods/{AAAA-MM}
└── categories/{categoria}
    ├── resumen, catálogos de filtros y fecha de corte
    ├── view_chunks/{seccion_fragmento}
    │   ├── positions: resumen por puesto
    │   ├── regions: ranking por región
    │   ├── courses: avance por curso
    │   ├── course_regions: avance de cada curso por región
    │   └── cube: puesto + región + curso para combinar filtros
    └── pending_chunks/{region_puesto_fragmento}
        └── persona + nombre + puesto + región + tienda + curso pendiente

dashboard_categories/{categoria}
└── periods/{como mapa}: resumen histórico para la gráfica mensual
```

RunSQL solo carga, calcula y publica. La web de reportes lee la sección que está
mostrando: no descarga colaboradores para pintar las gráficas y no consulta una
fila por persona. Para combinar varios filtros carga el cubo agregado de cada
categoría seleccionada. El detalle pendiente se solicita después, por región y
puesto, y se filtra dentro de esos bloques por persona o curso. `pendientes` es
`total - completados` y el porcentaje pendiente es `100 - porcentaje de avance`.
Al ejecutar `start --d(...)`, RunSQL muestra únicamente el estado y las
cantidades publicadas; no descarga ni renderiza nuevamente el resultado SQL.
El comando `show` conserva su previsualización limitada para validaciones.

Cada gráfica suele requerir solo el documento de resumen y uno o pocos bloques
de su sección. Los registros se agrupan en fragmentos menores a 1 MiB para
reducir escrituras, lecturas y transferencia. Volver a ejecutar la misma
categoría y mes reemplaza sus bloques anteriores; nunca se guarda un documento
de Firestore por cada fila del SQL.

### Encuesta de satisfacción

El proceso `Encuesta de satisfaccion.sql` recibe un solo archivo llamado
`Encuesta de satisfacción.xlsx`. Lee únicamente las pestañas de respuestas de
Competencias del Líder, LAC, Servicios Financieros, Entrevista, Óptica,
Conductores y HAMI; los tableros, síntesis y fórmulas auxiliares del libro no se
cargan.

Después de seleccionar el SQL y el Excel, publica el corte con el comando normal:

```text
start --d(2026-08-28)
```

RunSQL unifica programa, curso, instructor, región, rubros, recomendación y
comentario. DataStore recibe un cubo agregado para ISA/NPS, comentarios
recientes y conteos temáticos de fortalezas y oportunidades. El NPS sigue la
regla estándar de 0 a 10, pero la vista
muestra una advertencia mientras existan muchas respuestas con valor 5 para que
se valide si algún formulario histórico utilizó una escala distinta.

Dentro de cada bloque, `columns` contiene los nombres una sola vez y `rows`
contiene arreglos de valores. En los bloques de pendientes, `region` y
`position` también se guardan una sola vez en la cabecera. La web de reportes
debe conservar estos bloques en caché mientras el usuario cambia filtros para no
volver a facturar la misma lectura durante esa sesión.

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
