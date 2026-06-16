# syntax=docker/dockerfile:1
# =====================================================================
# Imagen del job ingest-public (Data Lake VisualRoad).
#
# El contexto de build es la RAIZ del repo, porque la imagen necesita la
# libreria compartida `libs/` ademas del `src/` del job. Construir asi:
#
#   docker build -f jobs/ingest-public/Dockerfile -t ingest-public .
#
# Ejecutar (Cloud Run pasa los args en cada ejecucion):
#   docker run --rm ingest-public --dataset culane --dry-run --limit 5
#
# Para una prueba LOCAL contra GCP hace falta: variables de entorno y ADC, p. ej.
#   docker run --rm --env-file .env \
#     -v "$HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro" \
#     -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
#     ingest-public --dataset culane --dry-run --limit 5
#   (en Windows el ADC esta en %APPDATA%\gcloud\application_default_credentials.json)

FROM python:3.12-slim

# Sin .pyc y salida sin buffer (logs inmediatos en Cloud Run).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias primero (capa cacheable mientras requirements no cambie).
COPY jobs/ingest-public/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Codigo: libreria compartida + fuente del job. (No se copian tests ni scripts.)
COPY libs/ ./libs/
COPY jobs/ingest-public/src/ ./src/

# Usuario no root.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# `python -m src.main` resuelve `src` y `libs` desde /app.
# Los argumentos (--dataset <nombre> [--dry-run] [--limit N]) se pasan en runtime.
ENTRYPOINT ["python", "-m", "src.main"]
