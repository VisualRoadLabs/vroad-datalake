"""Cliente GENERAL de Gemini en Vertex AI (compartido por classify, privacy, ...).

NO contiene logica de ningun job: ni taxonomias, ni prompts, ni validacion. Cada
job aporta su prompt y su `response_schema` y valida su propia respuesta. Apunta a
Vertex con `vertexai=True` -> usa la SA del job automaticamente, SIN claves (ADC).

API:
  - generate_json(image_bytes, prompt, schema): llamada SINCRONA; devuelve el JSON
    ya parseado (dict). Los transitorios (429 rate-limit / 503 / 5xx / timeouts) los
    reintenta el SDK con backoff exponencial + jitter (HttpRetryOptions, ver
    __init__); los errores permanentes (400/403/404) NO se reintentan.
  - media_resolution (def. "LOW"): calidad de la imagen enviada a Gemini; LOW =
    menos tokens por imagen = mas barato. Se puede subir por instancia (p. ej.
    privacy podria querer HIGH para caras/matriculas pequenas).

No hay modo lote: el batch de Vertex no admite peticiones inline (exige src en
GCS/BigQuery), lo que romperia el least-privilege de las SA (read-only en GCS, sin
tables.create). Para clasificar muchas imagenes se llama a generate_json en paralelo
con concurrencia ACOTADA en el job; el backoff del SDK absorbe los 429 residuales.
"""
from __future__ import annotations

import json
from typing import Dict, Optional

try:  # el SDK solo hace falta en runtime
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - solo si falta el paquete
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]


class GeminiVertex:
    """Cliente fino de Gemini en Vertex (sin claves: usa la SA). Sin logica de job."""

    def __init__(self, project: str, location: str, model: str, client=None, max_retries: int = 6,
                 media_resolution: str = "LOW"):
        self.project = project
        self.location = location
        self.model = model
        self.max_retries = max(1, max_retries)  # intentos TOTALES (incl. el original)
        self._media_resolution = self._resolve_media(media_resolution)
        if client is None:
            if genai is None:
                raise RuntimeError("google-genai no esta instalado; instala las dependencias.")
            # Reintento delegado al SDK: backoff exponencial + jitter, cap 60s, SOLO en
            # transitorios (429 rate-limit / 5xx / timeouts). Los 4xx permanentes no se
            # reintentan. flash-lite usa cuota compartida dinamica -> hay que absorber 429.
            http_options = types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=self.max_retries,
                    initial_delay=1.0,
                    max_delay=60.0,
                    exp_base=2.0,
                    jitter=1.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                )
            )
            client = genai.Client(vertexai=True, project=project, location=location,
                                  http_options=http_options)
        self._client = client

    @staticmethod
    def _resolve_media(name: str):
        if types is None:  # pragma: no cover - solo si falta el paquete
            return None
        levels = {
            "LOW": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "MEDIUM": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "HIGH": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        }
        return levels.get((name or "LOW").upper(), types.MediaResolution.MEDIA_RESOLUTION_LOW)

    def _config(self, schema: Optional[Dict]):
        return types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=schema,
            media_resolution=self._media_resolution,
            # No usamos function calling (salida JSON via response_schema). Apagar AFC
            # evita que el SDK lo active y lo anuncie ("AFC is enabled...") en cada llamada.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def generate_json(self, image_bytes: bytes, prompt: str, schema: Optional[Dict] = None,
                      mime_type: str = "image/jpeg") -> Dict:
        """Una imagen -> JSON parseado (dict).

        El reintento de transitorios (429/5xx/timeouts) lo hace el SDK con backoff
        exponencial + jitter (HttpRetryOptions de __init__). Aqui solo se hace la
        llamada y se parsea la respuesta (response_schema garantiza JSON valido).
        """
        contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt]
        resp = self._client.models.generate_content(
            model=self.model, contents=contents, config=self._config(schema)
        )
        return json.loads(resp.text)
