import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const projectRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..'
);

const backendDirectory = path.join(projectRoot, 'backend');
const virtualEnvironmentDirectory = path.join(
  backendDirectory,
  process.platform === 'win32' ? '.venv-windows' : '.venv'
);

export const virtualPython =
  process.platform === 'win32'
    ? path.join(virtualEnvironmentDirectory, 'Scripts', 'python.exe')
    : path.join(virtualEnvironmentDirectory, 'bin', 'python');

function run(command, args, options = {}) {
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
    env: {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1'
    },
    stdio: options.capture ? 'pipe' : 'inherit',
    shell: false,
    ...options
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const detail = options.capture
      ? `${result.stdout ?? ''}${result.stderr ?? ''}`.trim()
      : '';
    throw new Error(
      detail || `El comando ${command} terminó con código ${result.status}.`
    );
  }
  return result;
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

  if (!importStatus.ok) {
    const duckdbWindowsHelp =
      process.platform === 'win32' &&
      /duckdb[\s\S]*DLL load failed/i.test(importStatus.detail)
        ? '\n\nDuckDB necesita Microsoft Visual C++ Redistributable x64. Instálalo desde https://aka.ms/vs/17/release/vc_redist.x64.exe, cierra la terminal y vuelve a ejecutar npm.cmd run dev.'
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
