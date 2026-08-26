import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const projectRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..'
);

const backendDirectory = path.join(projectRoot, 'backend');
const windowsRuntimeDirectory = path.join(
  backendDirectory,
  '.runtime-windows'
);
const virtualEnvironmentDirectory = path.join(
  backendDirectory,
  process.platform === 'win32' ? '.venv-windows' : '.venv'
);

export const virtualPython =
  process.platform === 'win32'
    ? path.join(virtualEnvironmentDirectory, 'Scripts', 'python.exe')
    : path.join(virtualEnvironmentDirectory, 'bin', 'python');

const windowsRuntimePackage = {
  url: 'https://download.microsoft.com/download/4/7/c/47c6134b-d61f-4024-83bd-b9c9ea951c25/Microsoft.VCLibs.x64.14.00.Desktop.appx',
  sha256: 'b56a9101f706f9d95f815f5b7fa6efbac972e86573d378b96a07cff5540c5961'
};

export function backendEnvironment(overrides = {}) {
  const environment = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    ...overrides
  };

  if (process.platform === 'win32' && fs.existsSync(windowsRuntimeDirectory)) {
    environment.PATH = [
      windowsRuntimeDirectory,
      environment.PATH
    ].filter(Boolean).join(path.delimiter);
    environment.PYTHONPATH = [
      windowsRuntimeDirectory,
      environment.PYTHONPATH
    ].filter(Boolean).join(path.delimiter);
  }

  return environment;
}

function run(command, args, options = {}) {
  const { capture = false, env = {}, ...spawnOptions } = options;
  const isWindowsNpm = process.platform === 'win32' && command === 'npm';
  const executable = isWindowsNpm
    ? process.env.ComSpec || 'C:\\Windows\\System32\\cmd.exe'
    : command;
  const executableArgs = isWindowsNpm
    ? ['/d', '/s', '/c', 'npm.cmd', ...args]
    : args;
  const result = spawnSync(executable, executableArgs, {
    cwd: projectRoot,
    encoding: 'utf8',
    env: backendEnvironment(env),
    stdio: capture ? 'pipe' : 'inherit',
    shell: false,
    ...spawnOptions
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const detail = capture
      ? `${result.stdout ?? ''}${result.stderr ?? ''}`.trim()
      : '';
    throw new Error(
      detail || `El comando ${command} terminó con código ${result.status}.`
    );
  }
  return result;
}

function sha256(filePath) {
  const hash = createHash('sha256');
  hash.update(fs.readFileSync(filePath));
  return hash.digest('hex');
}

function windowsRuntimeIsReady() {
  const requiredFiles = [
    'msvcp140.dll',
    'msvcp140_1.dll',
    'msvcp140_2.dll',
    'vcruntime140.dll',
    'vcruntime140_1.dll',
    'sitecustomize.py'
  ];
  return requiredFiles.every((file) =>
    fs.existsSync(path.join(windowsRuntimeDirectory, file))
  );
}

function ensureWindowsRuntime() {
  if (process.platform !== 'win32' || windowsRuntimeIsReady()) return;
  if (process.arch !== 'x64') {
    throw new Error(
      `RunSQL todavía no incluye el runtime local para Windows ${process.arch}. Usa Node.js y Python x64.`
    );
  }

  console.log(
    'Preparando Microsoft Visual C++ Runtime local (sin permisos de administrador)...'
  );
  fs.rmSync(windowsRuntimeDirectory, { recursive: true, force: true });
  fs.mkdirSync(windowsRuntimeDirectory, { recursive: true });
  const packagePath = path.join(
    windowsRuntimeDirectory,
    'Microsoft.VCLibs.x64.14.00.Desktop.appx'
  );

  try {
    run(
      virtualPython,
      [
        '-c',
        [
          'import pathlib, shutil, urllib.request',
          `url = ${JSON.stringify(windowsRuntimePackage.url)}`,
          `destination = pathlib.Path(${JSON.stringify(packagePath)})`,
          "request = urllib.request.Request(url, headers={'User-Agent': 'RunSQL/0.1'})",
          'with urllib.request.urlopen(request, timeout=120) as response, destination.open(\'wb\') as output: shutil.copyfileobj(response, output)'
        ].join('\n')
      ],
      { capture: true }
    );

    const downloadedHash = sha256(packagePath);
    if (downloadedHash !== windowsRuntimePackage.sha256) {
      throw new Error(
        `la firma de contenido no coincide (SHA-256 ${downloadedHash})`
      );
    }

    run(
      virtualPython,
      [
        '-c',
        [
          'import pathlib, zipfile',
          `package = pathlib.Path(${JSON.stringify(packagePath)})`,
          `destination = pathlib.Path(${JSON.stringify(windowsRuntimeDirectory)})`,
          "wanted = lambda name: '/' not in name and name.lower().endswith('.dll')",
          'with zipfile.ZipFile(package) as archive: archive.extractall(destination, members=[name for name in archive.namelist() if wanted(name)])'
        ].join('\n')
      ],
      { capture: true }
    );

    fs.writeFileSync(
      path.join(windowsRuntimeDirectory, 'sitecustomize.py'),
      [
        'import os',
        'from pathlib import Path',
        '',
        '_runsql_dll_handle = None',
        "if os.name == 'nt' and hasattr(os, 'add_dll_directory'):",
        '    _runsql_dll_handle = os.add_dll_directory(str(Path(__file__).resolve().parent))',
        ''
      ].join('\n'),
      'utf8'
    );
    fs.rmSync(packagePath, { force: true });
  } catch (error) {
    fs.rmSync(windowsRuntimeDirectory, { recursive: true, force: true });
    throw new Error(
      `No se pudo preparar el runtime local de Microsoft sin administrador: ${error.message}`
    );
  }
}

function pythonVersion(command, prefixArgs = []) {
  try {
    const result = run(command, [...prefixArgs, '--version'], {
      capture: true
    });
    const text = `${result.stdout ?? ''} ${result.stderr ?? ''}`.trim();
    const match = text.match(/Python\s+(\d+)\.(\d+)/i);
    if (!match) return null;
    return {
      command,
      prefixArgs,
      major: Number(match[1]),
      minor: Number(match[2]),
      label: text
    };
  } catch {
    return null;
  }
}

function compatiblePython(version) {
  return Boolean(
    version &&
      version.major === 3 &&
      version.minor >= 9 &&
      version.minor <= 13
  );
}

function findSystemPython() {
  const candidates =
    process.platform === 'win32'
      ? [
          ['py', ['-3.13']],
          ['py', ['-3.12']],
          ['py', ['-3.11']],
          ['py', ['-3.10']],
          ['py', ['-3.9']],
          ['py', ['-3']],
          ['python', []],
          ['python3', []]
        ]
      : [
          ['python3.13', []],
          ['python3.12', []],
          ['python3.11', []],
          ['python3.10', []],
          ['python3.9', []],
          ['python3', []],
          ['python', []]
        ];

  for (const [command, prefixArgs] of candidates) {
    const version = pythonVersion(command, prefixArgs);
    if (compatiblePython(version)) {
      return version;
    }
  }

  throw new Error(
    'No se encontró una versión compatible de Python (3.9 a 3.13). Instala Python 3.12 desde https://www.python.org/downloads/ y vuelve a intentarlo.'
  );
}

function backendImportStatus() {
  if (!fs.existsSync(virtualPython)) {
    return {
      ok: false,
      detail: 'No existe el ejecutable del entorno virtual.'
    };
  }
  try {
    run(
      virtualPython,
      ['-c', 'import fastapi, uvicorn, multipart, duckdb, pandas, openpyxl'],
      { capture: true }
    );
    return { ok: true, detail: '' };
  } catch (error) {
    return { ok: false, detail: error.message };
  }
}

export function ensureBackendReady({ forceInstall = false } = {}) {
  const currentVersion = fs.existsSync(virtualPython)
    ? pythonVersion(virtualPython)
    : null;

  if (fs.existsSync(virtualPython) && !compatiblePython(currentVersion)) {
    const python = findSystemPython();
    console.log(
      `El entorno anterior usa ${currentVersion?.label ?? 'una versión desconocida'}. Reconstruyendo con ${python.label}...`
    );
    run(python.command, [
      ...python.prefixArgs,
      '-m',
      'venv',
      '--clear',
      virtualEnvironmentDirectory
    ]);
    forceInstall = true;
  } else if (!fs.existsSync(virtualPython)) {
    const python = findSystemPython();
    console.log(`Preparando backend con ${python.label}...`);
    run(python.command, [
      ...python.prefixArgs,
      '-m',
      'venv',
      virtualEnvironmentDirectory
    ]);
    forceInstall = true;
  }

  let importStatus = backendImportStatus();
  if (forceInstall || !importStatus.ok) {
    console.log('Instalando dependencias de Python...');
    run(virtualPython, [
      '-m',
      'pip',
      'install',
      '--disable-pip-version-check',
      '-r',
      path.join(backendDirectory, 'requirements.txt')
    ]);
    importStatus = backendImportStatus();
  }

  if (
    process.platform === 'win32' &&
    !importStatus.ok &&
    /duckdb[\s\S]*DLL load failed/i.test(importStatus.detail) &&
    !windowsRuntimeIsReady()
  ) {
    ensureWindowsRuntime();
    importStatus = backendImportStatus();
  }

  if (!importStatus.ok) {
    const duckdbWindowsHelp =
      process.platform === 'win32' &&
      /duckdb[\s\S]*DLL load failed/i.test(importStatus.detail)
        ? '\n\nRunSQL ya intentó cargar Visual C++ de forma local. No requiere administrador. Revisa que la seguridad de la empresa no haya bloqueado las DLL dentro de backend\\.runtime-windows.'
        : '';
    throw new Error(
      `El backend se instaló, pero una dependencia no se puede importar:\n${importStatus.detail}${duckdbWindowsHelp}`
    );
  }

  return virtualPython;
}

export function installFrontendDependencies() {
  console.log('Instalando dependencias del frontend...');
  run('npm', ['--prefix', 'frontend', 'install']);
}
