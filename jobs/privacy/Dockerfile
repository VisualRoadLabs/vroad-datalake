# syntax=docker/dockerfile:1
# =====================================================================
# Imagen del job privacy (anonimizacion de caras/matriculas, CPU, sin Gemini).
#
# El contexto de build es la RAIZ del repo, porque la imagen necesita `libs/`,
# el `src/` del job Y el modelo .pt (horneado, no se descarga en runtime).
#
# El modelo se baja a ./models/ ANTES del build:
#   - en CI lo hace el paso `fetch-model` de cloudbuild.yaml (la SA de build tiene
#     lectura sobre gs://bkt-prod-models-usc1).
#   - en local:
#       gcloud storage cp gs://bkt-prod-models-usc1/privacy/dashcam-anon-yolov8-v1.pt models/
#       docker build -f jobs/privacy/Dockerfile -t privacy .
#
# Ejecutar (Cloud Run pasa los args en cada ejecucion):
#   docker run --rm privacy --dry-run --limit 5

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Librerias de sistema que necesita opencv (lo arrastra ultralytics).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias primero (capa cacheable). torch CPU viene del extra-index del requirements.
COPY jobs/privacy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Codigo + modelo horneado.
COPY libs/ ./libs/
COPY jobs/privacy/src/ ./src/
COPY models/ ./models/

RUN useradd --create-home --uid 1000 appuser
USER appuser

# `python -m src.main` resuelve `src` y `libs` desde /app. El modelo se carga de
# /app/models/dashcam-anon-yolov8-v1.pt (override con env PRIVACY_MODEL_PATH).
ENTRYPOINT ["python", "-m", "src.main"]
