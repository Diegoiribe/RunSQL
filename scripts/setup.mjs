import {
  ensureBackendReady,
  installFrontendDependencies
} from './python-env.mjs';

try {
  installFrontendDependencies();
  ensureBackendReady({ forceInstall: true });
  console.log('\nRunSQL quedó preparado para esta computadora.');
} catch (error) {
  console.error(`\nNo se pudo preparar RunSQL: ${error.message}`);
  process.exitCode = 1;
}
