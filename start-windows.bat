@echo off
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo No se encontro Node.js. Instala Node.js 20 o posterior desde https://nodejs.org/
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo No se encontro npm. Reinstala Node.js y vuelve a intentarlo.
  pause
  exit /b 1
)

echo Preparando RunSQL...
call npm install
if errorlevel 1 goto error

call npm run setup
if errorlevel 1 goto error

echo.
echo RunSQL estara disponible en http://localhost:5173
call npm run dev
exit /b %errorlevel%

:error
echo.
echo No se pudo iniciar RunSQL. Revisa el mensaje anterior.
pause
exit /b 1
