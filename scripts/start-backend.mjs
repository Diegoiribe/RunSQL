import { spawn } from 'node:child_process';

import {
  backendEnvironment,
  ensureBackendReady,
  projectRoot
} from './python-env.mjs';

let child;

try {
  const python = ensureBackendReady();
  child = spawn(
    python,
    [
      '-m',
      'uvicorn',
      'backend.app.main:app',
      '--reload',
      '--host',
      '127.0.0.1',
      '--port',
      '8000'
    ],
    {
      cwd: projectRoot,
      env: backendEnvironment({ PYTHONUNBUFFERED: '1' }),
      stdio: 'inherit',
      shell: false
    }
  );
} catch (error) {
  console.error(`No se pudo iniciar el backend: ${error.message}`);
  process.exit(1);
}

const stop = (signal) => {
  if (child && !child.killed) child.kill(signal);
};

process.on('SIGINT', () => stop('SIGINT'));
process.on('SIGTERM', () => stop('SIGTERM'));

child.on('error', (error) => {
  console.error(`Error del backend: ${error.message}`);
  process.exitCode = 1;
});

child.on('exit', (code, signal) => {
  process.exitCode = signal ? 0 : code ?? 0;
});
