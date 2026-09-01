import { ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';

type CatalogFile = {
  key: string;
  name: string;
  table_name: string;
  required: boolean;
  aliases: string[];
  description: string;
};

type CatalogFamily = {
  key: string;
  name: string;
  table_name: string;
  required: boolean;
  min_files: number;
  prefixes: string[];
  description: string;
};

type Catalog = {
  files: CatalogFile[];
  families: CatalogFamily[];
  allowed_extensions: string[];
  max_file_size_mb: number;
  max_total_upload_mb: number;
  max_result_rows: number;
};

type ExecutionResult = {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  loaded_files: Array<{
    name: string;
    filename: string;
    table_name: string;
    rows: number;
    columns: string[];
  }>;
  output_files: Array<{
    filename: string;
    rows: number;
    size: number;
    content_base64: string;
  }>;
  firebase_publish: null | {
    project_id: string;
    period: string;
    category: string;
    category_label: string;
    collection_key: string | null;
    collection_label: string | null;
    cutoff_date: string;
    replaced_existing: boolean;
    same_cutoff_replacement: boolean;
    publication_revision: number;
    metric_rows: number;
    pending_rows: number;
    view_documents: number;
    detail_documents: number;
    documents_written: number;
  };
};

const API_URL = import.meta.env.VITE_API_URL ?? '';
const STARTER_SQL = `SELECT
  p.*
FROM almacenista_p AS p
LIMIT 100;`;

function normalize(value: string) {
  return value
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function fileStem(filename: string) {
  return filename.replace(/\.[^.]+$/, '');
}

function familyPeriodDisplayName(family: CatalogFamily, period: string) {
  const processName = family.name.replace(/\s+p\(x\)$/i, '');
  const prefix = normalize(processName).replace(/\s+/g, '_');
  return `${prefix}_p${period}`;
}

function csvCell(value: unknown) {
  const text = value === null || value === undefined ? '' : String(value);
  return `"${text.replaceAll('"', '""')}"`;
}

function formatElapsed(milliseconds: number) {
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  const millis = Math.floor(milliseconds % 1_000);
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(
    2,
    '0'
  )}.${String(millis).padStart(3, '0')}`;
}

function fileKind(file: File) {
  return file.name.toLowerCase().endsWith('.csv') ? 'CSV' : 'XLSX';
}

const SQL_TOKEN_PATTERN =
  /(--.*$|'(?:''|[^'])*'|\b(?:SELECT|FROM|WHERE|WITH|AS|LIMIT|JOIN|LEFT|RIGHT|FULL|INNER|OUTER|ON|USING|UNION|ALL|BY|GROUP|ORDER|HAVING|DISTINCT|CASE|WHEN|THEN|ELSE|END|AND|OR|NOT|NULL|IS|IN|LIKE|ASC|DESC|CAST|TRY_CAST|COALESCE|COUNT|SUM|MIN|MAX|AVG)\b|\b\d+(?:\.\d+)?\b)/gim;
const SQL_KEYWORDS = new Set(
  'SELECT FROM WHERE WITH AS LIMIT JOIN LEFT RIGHT FULL INNER OUTER ON USING UNION ALL BY GROUP ORDER HAVING DISTINCT CASE WHEN THEN ELSE END AND OR NOT NULL IS IN LIKE ASC DESC CAST TRY_CAST COALESCE COUNT SUM MIN MAX AVG'.split(
    ' '
  )
);

function highlightSql(value: string) {
  return value.split(SQL_TOKEN_PATTERN).map((token, index) => {
    if (/^--/.test(token))
      return (
        <span className="sql-comment" key={index}>
          {token}
        </span>
      );
    if (/^'/.test(token))
      return (
        <span className="sql-string" key={index}>
          {token}
        </span>
      );
    if (/^\d/.test(token))
      return (
        <span className="sql-number" key={index}>
          {token}
        </span>
      );
    if (SQL_KEYWORDS.has(token.toUpperCase()))
      return (
        <span className="sql-keyword" key={index}>
          {token}
        </span>
      );
    return token;
  });
}

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [uploads, setUploads] = useState<Record<string, File>>({});
  const [sql, setSql] = useState(STARTER_SQL);
  const [rulesSql, setRulesSql] = useState('');
  const [sqlFilename, setSqlFilename] = useState('consulta.sql');
  const [sqlExpanded, setSqlExpanded] = useState(false);
  const [sqlDragging, setSqlDragging] = useState(false);
  const [resourcesDragging, setResourcesDragging] = useState(false);
  const [resourcesExpanded, setResourcesExpanded] = useState(true);
  const [editingResourceKey, setEditingResourceKey] = useState<string | null>(
    null
  );
  const [editingFilename, setEditingFilename] = useState('');
  const [resourceMenu, setResourceMenu] = useState<{
    key: string;
    x: number;
    y: number;
  } | null>(null);
  const [terminalCommand, setTerminalCommand] = useState('');
  const [commandHistory, setCommandHistory] = useState<string[]>([]);
  const [commandHistoryIndex, setCommandHistoryIndex] = useState<number | null>(
    null
  );
  const [terminalLines, setTerminalLines] = useState<string[]>([
    'Escribe help para ver los comandos disponibles.'
  ]);
  const [resultHistoryLength, setResultHistoryLength] = useState(0);
  const commandHighlightRef = useRef<HTMLPreElement>(null);
  const commandInputRef = useRef<HTMLTextAreaElement>(null);
  const commandDraftRef = useRef('');
  const sqlFileRef = useRef<HTMLInputElement>(null);
  const resourceFileRef = useRef<HTMLInputElement>(null);
  const unassignedSequence = useRef(0);
  const [result, setResult] = useState<ExecutionResult | null>(null);
  const [previewTable, setPreviewTable] = useState<string | null>(null);
  const [sortColumn, setSortColumn] = useState<number | null>(null);
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [executionStartedAt, setExecutionStartedAt] = useState<number | null>(
    null
  );
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    if (!loading || executionStartedAt === null) return;
    const updateElapsed = () => setElapsedMs(Date.now() - executionStartedAt);
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 10);
    return () => window.clearInterval(timer);
  }, [loading, executionStartedAt]);

  useEffect(() => {
    fetch(`${API_URL}/api/catalog`)
      .then(async (response) => {
        if (!response.ok)
          throw new Error('No se pudo cargar el catálogo de archivos.');
        return response.json();
      })
      .then(setCatalog)
      .catch((reason: Error) => {
        const message = `${reason.message} Verifica que el backend esté activo.`;
        setError(message);
        setTerminalLines((lines) => [...lines, `error: ${message}`]);
      });
  }, []);

  const missingRequired = useMemo(() => {
    if (!catalog) return [];
    const missing = catalog.files
      .filter((item) => item.required && !uploads[item.key])
      .map((item) => item.name);
    for (const family of catalog.families) {
      const count = Object.keys(uploads).filter((key) =>
        key.startsWith(`${family.key}:p`)
      ).length;
      if (family.required && count < family.min_files)
        missing.push(family.name);
    }
    return missing;
  }, [catalog, uploads]);

  const generatedTables = useMemo(() => {
    const sourceTables = new Set([
      ...(catalog?.files.map((item) => item.table_name.toLowerCase()) ?? []),
      ...(catalog?.families.map((item) => item.table_name.toLowerCase()) ?? [])
    ]);
    return Array.from(
      rulesSql.matchAll(
        /create\s+or\s+replace\s+(?:table|view)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?/gi
      )
    )
      .map((match) => match[1])
      .filter(
        (table, index, tables) =>
          !sourceTables.has(table.toLowerCase()) &&
          tables.indexOf(table) === index
      );
  }, [catalog, rulesSql]);

  const sortedRows = useMemo(() => {
    if (!result || sortColumn === null) return result?.rows ?? [];
    return [...result.rows].sort((left, right) => {
      const first = left[sortColumn];
      const second = right[sortColumn];
      if (first === second) return 0;
      if (first === null || first === undefined) return 1;
      if (second === null || second === undefined) return -1;
      const firstNumber = typeof first === 'number' ? first : Number(first);
      const secondNumber = typeof second === 'number' ? second : Number(second);
      const comparison =
        Number.isFinite(firstNumber) && Number.isFinite(secondNumber)
          ? firstNumber - secondNumber
          : String(first).localeCompare(String(second), 'es', {
              numeric: true,
              sensitivity: 'base'
            });
      return sortDirection === 'asc' ? comparison : -comparison;
    });
  }, [result, sortColumn, sortDirection]);

  function definitionFor(file: File) {
    const stem = normalize(fileStem(file.name));
    const definition = catalog?.files.find((item) => {
      const names = [item.name, item.key, ...item.aliases].map(normalize);
      return names.includes(stem);
    });
    if (definition) return { key: definition.key };

    const periodMatch = stem.match(/^(.+?) p\s*(\d+)$/);
    if (!periodMatch) return undefined;
    const [, prefix, period] = periodMatch;
    const family = catalog?.families.find((item) =>
      item.prefixes.map(normalize).includes(prefix)
    );
    return family ? { key: `${family.key}:p${Number(period)}` } : undefined;
  }

  useEffect(() => {
    if (!catalog) return;
    setUploads((current) => {
      const next: Record<string, File> = {};
      let changed = false;
      for (const [key, file] of Object.entries(current)) {
        const definition = definitionFor(file);
        if (definition && !next[definition.key]) {
          next[definition.key] = file;
          if (definition.key !== key) changed = true;
        } else {
          let pendingKey = key.startsWith('unassigned:') ? key : '';
          if (!pendingKey || next[pendingKey]) {
            unassignedSequence.current += 1;
            pendingKey = `unassigned:${Date.now()}:${
              unassignedSequence.current
            }`;
          }
          next[pendingKey] = file;
          if (pendingKey !== key) changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [catalog]);

  function addFiles(files: FileList | File[]) {
    setError('');
    const next = { ...uploads };
    const pending: string[] = [];
    const rejected: string[] = [];
    const allowedExtensions = catalog?.allowed_extensions ?? [
      '.csv',
      '.xlsx',
      '.xlsm'
    ];
    for (const file of Array.from(files)) {
      const extension = file.name
        .slice(file.name.lastIndexOf('.'))
        .toLowerCase();
      if (!allowedExtensions.includes(extension)) {
        rejected.push(file.name);
        continue;
      }
      const definition = definitionFor(file);
      if (definition) next[definition.key] = file;
      else {
        unassignedSequence.current += 1;
        next[`unassigned:${Date.now()}:${unassignedSequence.current}`] = file;
        pending.push(file.name);
      }
    }
    setUploads(next);
    if (pending.length) {
      const instruction =
        sqlFilename === 'consulta.sql'
          ? 'carga el SQL para aplicar sus reglas'
          : 'revisa el nombre o renómbralo con doble clic';
      setTerminalLines((lines) => [
        ...lines,
        `pendiente: ${pending.join(', ')} · ${instruction}`
      ]);
    }
    if (rejected.length) {
      const message = `Formato no permitido: ${rejected.join(
        ', '
      )}. Usa Excel o CSV.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
    }
  }

  function loadResourceFiles(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) addFiles(event.target.files);
    event.target.value = '';
  }

  function beginRename(key: string, filename: string) {
    setResourceMenu(null);
    setEditingResourceKey(key);
    setEditingFilename(filename);
  }

  function renameResource(key: string) {
    const file = uploads[key];
    if (!file) return;
    const requestedName = editingFilename.trim();
    if (!requestedName) {
      setEditingResourceKey(null);
      return;
    }

    const originalExtension = file.name.match(/\.[^.]+$/)?.[0] ?? '';
    const filename = /\.[^.]+$/.test(requestedName)
      ? requestedName
      : `${requestedName}${originalExtension}`;
    const renamedFile = new File([file], filename, {
      type: file.type,
      lastModified: file.lastModified
    });
    const definition = definitionFor(renamedFile);
    let targetKey = definition?.key;
    if (!targetKey) {
      if (key.startsWith('unassigned:')) targetKey = key;
      else {
        unassignedSequence.current += 1;
        targetKey = `unassigned:${Date.now()}:${unassignedSequence.current}`;
      }
    }

    if (targetKey !== key && uploads[targetKey]) {
      const message = `Ya existe un archivo asignado como ${filename}.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
      return;
    }

    setUploads((current) => {
      const next = { ...current };
      delete next[key];
      next[targetKey] = renamedFile;
      return next;
    });
    setEditingResourceKey(null);
    setEditingFilename('');
    if (definition) {
      setError('');
      setTerminalLines((lines) => [...lines, `archivo asignado: ${filename}`]);
    } else {
      const message = `${filename} sigue sin coincidir con un nombre esperado.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `pendiente: ${message}`]);
    }
  }

  async function readSqlFile(file: File) {
    if (!file.name.toLowerCase().match(/\.(sql|txt)$/)) {
      setError('La terminal solo acepta archivos .sql o .txt.');
      return;
    }
    const contents = await file.text();
    setSql(contents);
    setRulesSql(contents);
    setTerminalCommand('');
    setSqlFilename(file.name);
    setSqlExpanded(false);
    const body = new FormData();
    body.append('sql', contents);
    body.append('filename', file.name);
    try {
      const response = await fetch(`${API_URL}/api/analyze-sql`, {
        method: 'POST',
        body
      });
      const payload = await response.json();
      if (!response.ok)
        throw new Error(
          payload.detail ?? 'No se pudieron obtener las reglas del SQL.'
        );
      setCatalog(payload);
      setError('');
      setTerminalLines((lines) => [
        ...lines.filter((line) => !line.startsWith('pendiente:')),
        `archivo cargado: ${file.name}`,
        `reglas listas: ${payload.files.length} documento(s) y ${payload.families.length} familia(s) de periodos`
      ]);
    } catch (reason) {
      const message =
        reason instanceof Error
          ? reason.message
          : 'No se pudieron cargar las reglas del SQL.';
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
    }
  }

  async function loadSqlFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) await readSqlFile(file);
    event.target.value = '';
  }

  async function execute(
    statementOverride?: string,
    preview?: { table: string; limit: number },
    publish?: { cutoffDate: string; name?: string; collectionLink?: string; reportType?: string }
  ) {
    const statement = statementOverride ?? sql;
    const unassignedFiles = Object.entries(uploads)
      .filter(([key]) => key.startsWith('unassigned:'))
      .map(([, file]) => file.name);
    if (unassignedFiles.length) {
      const message = `Renombra antes de ejecutar: ${unassignedFiles.join(
        ', '
      )}. Haz doble clic sobre el nombre.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
      return;
    }
    if (missingRequired.length) {
      const message = `Faltan: ${missingRequired.join(', ')}.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
      return;
    }
    const totalUploadBytes = Object.values(uploads).reduce(
      (total, file) => total + file.size,
      0
    );
    if (
      catalog &&
      totalUploadBytes > catalog.max_total_upload_mb * 1024 * 1024
    ) {
      const message = `Los archivos juntos superan el límite de ${catalog.max_total_upload_mb} MB para esta ejecución.`;
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
      return;
    }
    const startedAt = Date.now();
    setElapsedMs(0);
    setExecutionStartedAt(startedAt);
    setLoading(true);
    setError('');
    setResult(null);
    setPreviewTable(preview?.table ?? null);
    setSortColumn(null);
    setSortDirection('asc');
    const body = new FormData();
    body.append('sql', statement);
    body.append('rules_sql', rulesSql || statement);
    body.append('sql_filename', sqlFilename);
    if (preview) {
      body.append('preview_table', preview.table);
      body.append('preview_limit', String(preview.limit));
    }
    if (publish) {
      body.append('cutoff_date', publish.cutoffDate);
      body.append('publish_to_firebase', 'true');
      if (publish.name) body.append('publication_name', publish.name);
      if (publish.collectionLink)
        body.append('collection_link', publish.collectionLink);
      if (publish.reportType) body.append('report_type', publish.reportType);
    }
    Object.values(uploads).forEach((file) => body.append('files', file));
    try {
      const response = await fetch(`${API_URL}/api/execute`, {
        method: 'POST',
        body
      });
      const payload = await response.json();
      if (!response.ok)
        throw new Error(payload.detail ?? 'No se pudo ejecutar la consulta.');
      setResult(payload);
      const duration = Date.now() - startedAt;
      const outputMessage = payload.firebase_publish
        ? `${payload.firebase_publish.replaced_existing ? 'Corte reemplazado' : 'Corte publicado'}: ${payload.firebase_publish.category_label}${payload.firebase_publish.collection_label ? ` · colección ${payload.firebase_publish.collection_label}` : ''} · ${payload.firebase_publish.metric_rows.toLocaleString()} indicadores y ${payload.firebase_publish.pending_rows.toLocaleString()} pendientes indexados`
        : payload.output_files?.length
        ? `${payload.output_files.length} archivo(s) Excel generado(s)`
        : `${payload.row_count.toLocaleString()} filas obtenidas`;
      setTerminalLines((lines) => {
        const nextLines = [
          ...lines,
          `listo en ${formatElapsed(duration)}: ${outputMessage}`
        ];
        setResultHistoryLength(nextLines.length);
        return nextLines;
      });
    } catch (reason) {
      const message =
        reason instanceof Error
          ? reason.message
          : 'Ocurrió un error inesperado.';
      setError(message);
      setTerminalLines((lines) => [...lines, `error: ${message}`]);
    } finally {
      setElapsedMs(Date.now() - startedAt);
      setExecutionStartedAt(null);
      setLoading(false);
    }
  }

  function downloadGeneratedFile(
    file: ExecutionResult['output_files'][number]
  ) {
    const binary = window.atob(file.content_base64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1)
      bytes[index] = binary.charCodeAt(index);
    const url = URL.createObjectURL(
      new Blob([bytes], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      })
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = file.filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  }

  function downloadCsv() {
    if (!result) return;
    const csv = [result.columns, ...result.rows]
      .map((row) => row.map(csvCell).join(','))
      .join('\n');
    const blob = new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `resultado-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const resourceEntries = Object.entries(uploads).map(([key, file]) => {
    const family = catalog?.families.find((item) =>
      key.startsWith(`${item.key}:p`)
    );
    const definition = catalog?.files.find((item) => item.key === key);
    const period = family ? key.match(/:p(\d+)$/)?.[1] ?? null : null;
    return {
      key,
      file,
      tableName:
        family && period
          ? familyPeriodDisplayName(family, period)
          : definition?.table_name ?? 'archivo',
      description: '',
      assigned: Boolean(family || definition)
    };
  });
  const selectedResource = resourceMenu ? uploads[resourceMenu.key] : null;
  const hasLoadedSql = sqlFilename !== 'consulta.sql';
  const documentCount = resourceEntries.length + (hasLoadedSql ? 1 : 0);
  const processLabel = hasLoadedSql
    ? fileStem(sqlFilename)
        .replace(/\s*ejemplo$/i, '')
        .trim()
    : 'Documentos';

  function runTerminalCommand() {
    const rawCommand = terminalCommand.trim();
    const command = rawCommand.toLowerCase();
    if (!rawCommand) return;
    setCommandHistory((history) =>
      history.at(-1) === rawCommand ? history : [...history, rawCommand]
    );
    setCommandHistoryIndex(null);
    commandDraftRef.current = '';
    setTerminalCommand('');

    if (command === 'clear') {
      setTerminalLines([]);
      setResultHistoryLength(0);
      setResult(null);
      return;
    }

    setTerminalLines((lines) => [...lines, `runsql % ${rawCommand}`]);
    if (command === 'upload') {
      sqlFileRef.current?.click();
    } else if (/^start\b/i.test(rawCommand)) {
      const optionMatches = [
        ...rawCommand.matchAll(/--([dnlt])\(([^()]*)\)/gi)
      ];
      const residue = rawCommand
        .replace(/^start\b/i, '')
        .replace(/--[dnlt]\([^()]*\)/gi, '')
        .trim();
      const options = new Map<string, string>();
      let optionError = residue ? `opción no reconocida: ${residue}` : '';
      for (const match of optionMatches) {
        const key = match[1].toLowerCase();
        const value = match[2].trim();
        if (options.has(key)) optionError = `la opción --${key} está repetida`;
        else if (!value) optionError = `la opción --${key} no puede estar vacía`;
        options.set(key, value);
      }
      const cutoffDate = options.get('d') ?? '';
      if (!optionError && !/^\d{4}-\d{2}-\d{2}$/.test(cutoffDate))
        optionError = 'falta --d(AAAA-MM-DD)';
      const reportType = (options.get('t') ?? '').toLowerCase();
      if (!optionError && reportType && !['c', 's', 'e'].includes(reportType))
        optionError = '--t solo admite c, s o e';
      if (optionError) {
        setTerminalLines((lines) => [
          ...lines,
          `error: ${optionError}. Ejemplo: start --d(2026-07-30) --t(e) --n(EIC Presupuesto) --l(Staff)`
        ]);
        return;
      }
      if (!catalog)
        setTerminalLines((lines) => [
          ...lines,
          'error: el backend todavía no está disponible'
        ]);
      else if (loading)
        setTerminalLines((lines) => [
          ...lines,
          'la consulta ya se está ejecutando'
        ]);
      else
        void execute(undefined, undefined, {
          cutoffDate,
          name: options.get('n'),
          collectionLink: options.get('l'),
          reportType: reportType || undefined
        });
    } else if (command === 'help') {
      setTerminalLines((lines) => [
        ...lines,
        'upload                    abrir un archivo SQL',
        'start --d(fecha) [--t(c|s|e)] [--n(nombre)] [--l(colección)]',
        'show tables               listar tablas generadas',
        'show <tabla> limit <x>     previsualizar y ordenar una tabla',
        'SELECT / WITH              ejecutar una consulta',
        'download                   descargar el resultado',
        'clear                      limpiar la terminal',
        '↑ / ↓                      recorrer comandos anteriores',
        'help                       mostrar esta ayuda'
      ]);
    } else if (command === 'show tables') {
      setTerminalLines((lines) => [
        ...lines,
        generatedTables.length
          ? `tablas generadas:\n  ${generatedTables.join('\n  ')}`
          : 'no se detectaron tablas generadas'
      ]);
    } else if (/^show\b/i.test(rawCommand)) {
      const showMatch = rawCommand.match(
        /^show(?:\s+tabla)?\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+limit\s+(\d+)$/i
      );
      if (!showMatch) {
        setTerminalLines((lines) => [
          ...lines,
          'error: usa show <tabla> limit <x>; ejemplo: show detalle_colaborador_almacenista limit 20'
        ]);
      } else if (!rulesSql.trim()) {
        setTerminalLines((lines) => [
          ...lines,
          'error: primero carga un archivo SQL'
        ]);
      } else if (loading) {
        setTerminalLines((lines) => [
          ...lines,
          'la consulta ya se está ejecutando'
        ]);
      } else {
        const limit = Math.max(1, Math.min(Number(showMatch[2]), 1_000));
        void execute(rulesSql, { table: showMatch[1], limit });
      }
    } else if (command === 'download') {
      if (result?.output_files?.length)
        result.output_files.forEach(downloadGeneratedFile);
      else if (result) downloadCsv();
      else
        setTerminalLines((lines) => [
          ...lines,
          'error: todavía no hay un resultado para descargar'
        ]);
    } else if (/^(select|with)\b/i.test(rawCommand)) {
      setSql(rawCommand);
      if (!catalog)
        setTerminalLines((lines) => [
          ...lines,
          'error: el backend todavía no está disponible'
        ]);
      else if (loading)
        setTerminalLines((lines) => [
          ...lines,
          'la consulta ya se está ejecutando'
        ]);
      else void execute(rawCommand);
    } else {
      setTerminalLines((lines) => [
        ...lines,
        `comando no encontrado: ${rawCommand}`
      ]);
    }
  }

  function renderTerminalHistory(lines: string[], offset = 0) {
    return lines.map((line, index) => (
      <div
        className={line.startsWith('error:') ? 'console-error' : ''}
        key={`${line}-${offset + index}`}
      >
        {line.startsWith('runsql % ') ? (
          <>
            <span className="history-prompt">runsql %</span>{' '}
            {highlightSql(line.slice(9))}
          </>
        ) : (
          line
        )}
      </div>
    ));
  }

  return (
    <main className="app-page">
      <header className="hero">
        <div className="hero-copy">
          <span className="eyebrow">Procesador de datos</span>
          <h1>
            Tus archivos.
            <br />
            <em>Una consulta.</em>
          </h1>
          <p>
            Sube los insumos con el nombre correcto, agrega tu SQL y obtén un
            resultado listo para descargar.
          </p>
        </div>
        <div className="steps">
          <span className="active">01 Archivos</span>
          <span>02 Consulta</span>
          <span>03 Resultado</span>
        </div>
      </header>

      <section className="workbench">
        <aside
          className={`resource-sidebar ${
            resourcesDragging ? 'is-dragging' : ''
          }`}
          onDragEnter={(event) => {
            event.preventDefault();
            setResourcesDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node))
              setResourcesDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setResourcesDragging(false);
            addFiles(event.dataTransfer.files);
          }}
        >
          <header className="library-header">
            <div>
              <span className="section-label">Recursos</span>
              <h1>Biblioteca</h1>
              <small className="library-total">
                {documentCount}{' '}
                {documentCount === 1 ? 'documento' : 'documentos'}
              </small>
            </div>
          </header>

          <div className="resource-list">
            {documentCount ? (
              <section className="resource-group">
                <button
                  className="resource-group-header"
                  type="button"
                  onClick={() => setResourcesExpanded((expanded) => !expanded)}
                  aria-expanded={resourcesExpanded}
                >
                  <span
                    className={`resource-group-chevron ${
                      resourcesExpanded ? 'is-open' : ''
                    }`}
                  >
                    ›
                  </span>
                  <span className="process-file-icon" aria-hidden="true">
                    <svg viewBox="0 0 32 40">
                      <path
                        className="docs-page"
                        d="M5 1h14l8 8v28a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2Z"
                      />
                      <path
                        className="docs-fold"
                        d="M19 1v7a2 2 0 0 0 2 2h6Z"
                      />
                      <path
                        className="docs-lines"
                        d="M8 17h14M8 22h14M8 27h10"
                      />
                    </svg>
                  </span>
                  <strong>{processLabel}</strong>
                </button>
                {resourcesExpanded && (
                  <div className="resource-group-children">
                    {hasLoadedSql && (
                      <article className="sql-resource-row">
                        <span className="sql-file-icon" aria-hidden="true">
                          SQL
                        </span>
                        <strong>{sqlFilename}</strong>
                      </article>
                    )}
                    {resourceEntries.map(
                      ({ key, file, tableName, description, assigned }) => (
                        <article
                          className={`resource-card ${
                            assigned ? '' : 'is-unassigned'
                          }`}
                          key={key}
                          onDoubleClick={() =>
                            editingResourceKey !== key &&
                            beginRename(key, file.name)
                          }
                          onContextMenu={(event) => {
                            event.preventDefault();
                            const menuWidth = 160;
                            const menuHeight = 62;
                            setResourceMenu({
                              key,
                              x: Math.min(
                                event.clientX,
                                window.innerWidth - menuWidth - 10
                              ),
                              y: Math.min(
                                event.clientY,
                                window.innerHeight - menuHeight - 10
                              )
                            });
                          }}
                        >
                          <span
                            className={`file-type-icon ${
                              fileKind(file) === 'XLSX' ? 'is-excel' : ''
                            }`}
                            aria-hidden="true"
                          >
                            {fileKind(file) === 'XLSX' ? (
                              <svg
                                viewBox="0 0 32 40"
                                role="img"
                                aria-label="Google Sheets"
                              >
                                <path
                                  className="sheet-page"
                                  d="M5 1h14l8 8v28a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2Z"
                                />
                                <path
                                  className="sheet-fold"
                                  d="M19 1v7a2 2 0 0 0 2 2h6Z"
                                />
                                <path
                                  className="sheet-grid"
                                  d="M8 16h14v14H8Zm0 4.7h14M8 25.3h14M12.7 16v14M17.3 16v14"
                                />
                              </svg>
                            ) : (
                              fileKind(file)
                            )}
                          </span>
                          <div className="resource-copy">
                            {editingResourceKey === key ? (
                              <input
                                className="resource-name-input"
                                value={editingFilename}
                                onChange={(event) =>
                                  setEditingFilename(event.target.value)
                                }
                                onBlur={() => renameResource(key)}
                                onKeyDown={(event) => {
                                  if (event.key === 'Enter')
                                    renameResource(key);
                                  if (event.key === 'Escape')
                                    setEditingResourceKey(null);
                                }}
                                onFocus={(event) => {
                                  const extensionIndex =
                                    event.currentTarget.value.lastIndexOf('.');
                                  event.currentTarget.setSelectionRange(
                                    0,
                                    extensionIndex > 0
                                      ? extensionIndex
                                      : event.currentTarget.value.length
                                  );
                                }}
                                aria-label={`Renombrar ${file.name}`}
                                onClick={(event) => event.stopPropagation()}
                                onDoubleClick={(event) =>
                                  event.stopPropagation()
                                }
                                autoFocus
                              />
                            ) : (
                              <strong title="Doble clic para renombrar">
                                {file.name}
                              </strong>
                            )}
                            {description && <small>{description}</small>}
                            <code className={assigned ? '' : 'pending-badge'}>
                              {assigned ? tableName : 'Pendiente'}
                            </code>
                          </div>
                        </article>
                      )
                    )}
                  </div>
                )}
              </section>
            ) : (
              <button
                className="empty-library"
                type="button"
                onClick={() => resourceFileRef.current?.click()}
              >
                <span className="empty-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24">
                    <path d="M12 17V5M7.5 9.5 12 5l4.5 4.5" />
                  </svg>
                </span>
                <strong>Aún no hay recursos</strong>
                <p>Agrega tus archivos Excel o CSV para preparar las tablas.</p>
                <small>Haz clic para seleccionar</small>
              </button>
            )}
          </div>

          <input
            ref={resourceFileRef}
            className="hidden-resource-input"
            type="file"
            multiple
            accept=".csv,.xlsx,.xlsm"
            onChange={loadResourceFiles}
          />
          <button
            className="add-resources"
            type="button"
            onClick={() => resourceFileRef.current?.click()}
          >
            <span>＋</span> Agregar recursos
          </button>
          {resourcesDragging && (
            <div className="resource-drop-overlay">
              <strong>Suelta tus recursos aquí</strong>
              <small>Excel y CSV</small>
            </div>
          )}
        </aside>

        <section className="terminal-pane">
          <header className="terminal-toolbar">
            <div className="terminal-dots">
              <span></span>
              <span></span>
              <span></span>
            </div>
            <span className="terminal-prompt">›_</span>
            <strong className="terminal-filename">{sqlFilename}</strong>
            <span className={`terminal-mode ${loading ? 'is-running' : ''}`}>
              {loading ? 'RUNNING' : result ? 'DONE' : 'READY'}
              {(loading || result) && <time>{formatElapsed(elapsedMs)}</time>}
            </span>
            <input
              ref={sqlFileRef}
              className="hidden-sql-input"
              type="file"
              accept=".sql,.txt"
              onChange={loadSqlFile}
            />
          </header>

          <div className="terminal-content">
            <div
              className={`terminal-session ${sqlDragging ? 'is-dragging' : ''}`}
              onDragEnter={(event) => {
                event.preventDefault();
                setSqlDragging(true);
              }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node))
                  setSqlDragging(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setSqlDragging(false);
                const file = event.dataTransfer.files[0];
                if (file) void readSqlFile(file);
              }}
            >
              <div className="terminal-scroll" aria-live="polite">
                <div className="terminal-history">
                  {renderTerminalHistory(
                    result
                      ? terminalLines.slice(0, resultHistoryLength)
                      : terminalLines
                  )}
                </div>

                {sqlFilename !== 'consulta.sql' && (
                  <div
                    className={`loaded-sql ${sqlExpanded ? 'is-expanded' : ''}`}
                  >
                    <button
                      className="loaded-sql-toggle"
                      type="button"
                      onClick={() => setSqlExpanded((expanded) => !expanded)}
                      aria-expanded={sqlExpanded}
                    >
                      <span className="loaded-sql-arrow">›</span>
                      <strong>{sqlFilename}</strong>
                      <small>{sql.split(/\r?\n/).length} líneas</small>
                    </button>
                    {sqlExpanded && (
                      <pre className="loaded-sql-code">{highlightSql(sql)}</pre>
                    )}
                  </div>
                )}

                {result && (
                  <div className="terminal-result">
                    {result.firebase_publish && (
                      <div className="firebase-publish-status">
                        <span>
                          {result.firebase_publish.replaced_existing
                            ? 'Corte reemplazado'
                            : 'Firebase actualizado'}
                        </span>
                        <strong>
                          {result.firebase_publish.category} ·{' '}
                          {result.firebase_publish.cutoff_date}
                        </strong>
                        <small>
                          revisión {result.firebase_publish.publication_revision} ·{' '}
                          {result.firebase_publish.view_documents.toLocaleString()}{' '}
                          secciones ·{' '}
                          {result.firebase_publish.pending_rows.toLocaleString()}{' '}
                          pendientes en{' '}
                          {result.firebase_publish.detail_documents.toLocaleString()}{' '}
                          bloques ·{' '}
                          {result.firebase_publish.documents_written.toLocaleString()}{' '}
                          documentos
                        </small>
                      </div>
                    )}
                    {result.output_files?.length > 0 && (
                      <div className="generated-outputs">
                        <div className="generated-outputs-heading">
                          <span>Archivos generados</span>
                          <small>{result.output_files.length} Excel</small>
                        </div>
                        {result.output_files.map((file) => (
                          <button
                            key={file.filename}
                            type="button"
                            onClick={() => downloadGeneratedFile(file)}
                          >
                            <span className="generated-sheet-icon">XLSX</span>
                            <span className="generated-file-copy">
                              <strong>{file.filename}</strong>
                              <small>
                                {file.rows.toLocaleString()} filas ·{' '}
                                {(file.size / 1024).toFixed(1)} KB
                              </small>
                            </span>
                            <span className="generated-download">↓</span>
                          </button>
                        ))}
                      </div>
                    )}
                    <div className="terminal-result-meta">
                      {previewTable
                        ? `Preview: ${previewTable}`
                        : result.output_files?.length
                        ? 'Última validación'
                        : 'Resultado'}{' '}
                      · {result.row_count.toLocaleString()} filas
                      {result.truncated ? '+' : ''} · escribe clear para limpiar
                    </div>
                    <div className="terminal-table-scroll">
                      <table>
                        <thead>
                          <tr>
                            {result.columns.map((column, index) => (
                              <th key={`${column}-${index}`}>
                                <button
                                  type="button"
                                  onClick={() => {
                                    if (sortColumn === index)
                                      setSortDirection((direction) =>
                                        direction === 'asc' ? 'desc' : 'asc'
                                      );
                                    else {
                                      setSortColumn(index);
                                      setSortDirection('asc');
                                    }
                                  }}
                                >
                                  <span>{column}</span>
                                  <span
                                    className={
                                      sortColumn === index
                                        ? 'sort-indicator is-active'
                                        : 'sort-indicator'
                                    }
                                  >
                                    {sortColumn === index
                                      ? sortDirection === 'asc'
                                        ? '↑'
                                        : '↓'
                                      : '↕'}
                                  </span>
                                </button>
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {sortedRows.slice(0, 1_000).map((row, rowIndex) => (
                            <tr key={rowIndex}>
                              {row.map((value, columnIndex) => (
                                <td key={columnIndex}>
                                  {value === null ? (
                                    <span className="null">NULL</span>
                                  ) : (
                                    String(value)
                                  )}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {result && terminalLines.length > resultHistoryLength && (
                  <div className="terminal-history terminal-history-after-result">
                    {renderTerminalHistory(
                      terminalLines.slice(resultHistoryLength),
                      resultHistoryLength
                    )}
                  </div>
                )}

                <div className="command-line">
                  <span>runsql %</span>
                  <div className="command-editor">
                    <pre ref={commandHighlightRef} aria-hidden="true">
                      {highlightSql(terminalCommand)}
                      <br />
                    </pre>
                    <textarea
                      ref={commandInputRef}
                      value={terminalCommand}
                      onChange={(event) => {
                        const value = event.target.value;
                        setTerminalCommand(value);
                        setCommandHistoryIndex(null);
                        commandDraftRef.current = value;
                      }}
                      onScroll={(event) => {
                        if (!commandHighlightRef.current) return;
                        commandHighlightRef.current.scrollTop =
                          event.currentTarget.scrollTop;
                        commandHighlightRef.current.scrollLeft =
                          event.currentTarget.scrollLeft;
                      }}
                      onKeyDown={(event) => {
                        const input = event.currentTarget;
                        const cursorStart = input.selectionStart;
                        const cursorEnd = input.selectionEnd;
                        const cursorOnFirstLine = !terminalCommand
                          .slice(0, cursorStart)
                          .includes('\n');
                        const cursorOnLastLine = !terminalCommand
                          .slice(cursorEnd)
                          .includes('\n');

                        if (
                          event.key === 'ArrowUp' &&
                          cursorOnFirstLine &&
                          commandHistory.length
                        ) {
                          event.preventDefault();
                          if (commandHistoryIndex === null)
                            commandDraftRef.current = terminalCommand;
                          const nextIndex =
                            commandHistoryIndex === null
                              ? commandHistory.length - 1
                              : Math.max(0, commandHistoryIndex - 1);
                          const previousCommand = commandHistory[nextIndex];
                          setCommandHistoryIndex(nextIndex);
                          setTerminalCommand(previousCommand);
                          window.requestAnimationFrame(() => {
                            const target = commandInputRef.current;
                            target?.setSelectionRange(
                              previousCommand.length,
                              previousCommand.length
                            );
                          });
                          return;
                        }

                        if (
                          event.key === 'ArrowDown' &&
                          cursorOnLastLine &&
                          commandHistoryIndex !== null
                        ) {
                          event.preventDefault();
                          const nextIndex = commandHistoryIndex + 1;
                          const nextCommand =
                            nextIndex >= commandHistory.length
                              ? commandDraftRef.current
                              : commandHistory[nextIndex];
                          setCommandHistoryIndex(
                            nextIndex >= commandHistory.length
                              ? null
                              : nextIndex
                          );
                          setTerminalCommand(nextCommand);
                          window.requestAnimationFrame(() => {
                            const target = commandInputRef.current;
                            target?.setSelectionRange(
                              nextCommand.length,
                              nextCommand.length
                            );
                          });
                          return;
                        }

                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault();
                          runTerminalCommand();
                        }
                      }}
                      autoCapitalize="none"
                      autoComplete="off"
                      spellCheck={false}
                      aria-label="Comando de terminal"
                      placeholder="Escribe SQL, upload o help"
                    />
                  </div>
                </div>
              </div>
              {sqlDragging && (
                <div className="sql-drop-overlay">
                  <strong>Suelta tu consulta</strong>
                  <small>Archivo .sql o .txt</small>
                </div>
              )}
            </div>
          </div>
        </section>
      </section>
      {resourceMenu && selectedResource && (
        <div
          className="resource-context-backdrop"
          role="presentation"
          onMouseDown={() => setResourceMenu(null)}
          onContextMenu={(event) => {
            event.preventDefault();
            setResourceMenu(null);
          }}
        >
          <div
            className="resource-context-menu"
            role="menu"
            aria-label={`Opciones de ${selectedResource.name}`}
            style={{ left: resourceMenu.x, top: resourceMenu.y }}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              className="danger"
              type="button"
              role="menuitem"
              onClick={() => {
                setUploads(({ [resourceMenu.key]: _, ...rest }) => rest);
                setResourceMenu(null);
              }}
            >
              <span className="context-icon" aria-hidden="true">
                <svg viewBox="0 0 20 20">
                  <path d="M4 6h12M8 3.5h4M6 6l.7 10.5h6.6L14 6M8.3 8.5v5.5M11.7 8.5v5.5" />
                </svg>
              </span>{' '}
              Eliminar
            </button>
          </div>
        </div>
      )}
      <footer>
        Los archivos se procesan en memoria y no se guardan en el servidor.
      </footer>
    </main>
  );
}
