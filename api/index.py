from backend.app.main import app


# Vercel detecta esta instancia de FastAPI y crea la función automáticamente.
handler = app
