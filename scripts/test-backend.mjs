import { spawnSync } from 'node:child_process';

import {
  backendEnvironment,
  ensureBackendReady,
  projectRoot
} from './python-env.mjs';

try {
  const python = ensureBackendReady();
  const result = spawnSync(
    python,
    ['-m', 'unittest', 'discover', '-s', 'backend/tests'],
    {
      cwd: projectRoot,
      env: backendEnvironment({ PYTHONPATH: 'backend' }),
      stdio: 'inherit',
      shell: false
    }
  );
  process.exitCode = result.status ?? 1;
} catch (error) {
  console.error(`No se pudieron ejecutar las pruebas: ${error.message}`);
  process.exitCode = 1;
}
