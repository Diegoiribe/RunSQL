#!/bin/sh

cd "$(dirname "$0")" || exit 1

if ! command -v node >/dev/null 2>&1; then
  echo "No se encontró Node.js. Instala Node.js 20 o posterior desde https://nodejs.org/"
  printf "Presiona Enter para cerrar..."
  read -r _answer
  exit 1
fi

echo "Preparando RunSQL..."
npm install || exit 1
npm run setup || exit 1

echo ""
echo "RunSQL estará disponible en http://localhost:5173"
npm run dev
