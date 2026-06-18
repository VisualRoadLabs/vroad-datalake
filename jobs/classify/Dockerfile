# syntax=docker/dockerfile:1
# =====================================================================
# Imagen del job classify (Data Lake VisualRoad).
#
# El contexto de build es la RAIZ del repo, porque la imagen necesita la
# libreria compartida `libs/` ademas del `src/` del job. Construir asi:
#
#   docker build -f jobs/classify/Dockerfile -t classify .
#
# Ejecutar (Cloud Run pasa los args en cada ejecucion):
#   docker run --rm classify --source public --dataset culane --dry-run --limit 5
#
# Para una prueba LOCAL contra GCP hacen falta variables de entorno y ADC, p. ej.
#   docker run --rm --env-file .env \
#     -v "$HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro" \
#     -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
#     classify --source public --dataset culane --dry-run --limit 5
#   (en Windows el ADC esta en %APPDATA%\gcloud\application_default_credentials.json)
# Vertex se invoca con la SA del job (sin claves); en Cloud Run no hace falta CA.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY jobs/classify/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY libs/ ./libs/
COPY jobs/classify/src/ ./src/

RUN useradd --create-home --uid 1000 appuser
USER appuser

# `python -m src.main` resuelve `src` y `libs` desde /app.
# Argumentos en runtime: --source public|user [--dataset N] [--dry-run] [--limit N].
ENTRYPOINT ["python", "-m", "src.main"]
