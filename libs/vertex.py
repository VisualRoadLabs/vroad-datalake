"""Cliente GENERAL de Gemini en Vertex AI (compartido por classify, privacy, ...).

NO contiene logica de ningun job: ni taxonomias, ni prompts, ni validacion. Cada
job aporta su prompt y su `response_schema` y valida su propia respuesta. Apunta a
Vertex con `vertexai=True` -> usa la SA del job automaticamente, SIN claves (ADC).

API:
  - generate_json(image_bytes, prompt, schema): llamada SINCRONA; devuelve el JSON
    ya parseado (dict). Reintenta transitorios.
  - media_resolution (def. "LOW"): calidad de la imagen enviada a Gemini; LOW =
    menos tokens por imagen = mas barato. Se puede subir por instancia (p. ej.
    privacy podria querer HIGH para caras/matriculas pequenas).

No hay modo lote: el batch de Vertex no admite peticiones inline (exige src en
GCS/BigQuery), lo que romperia el least-privilege de las SA (read-only en GCS, sin
tables.create). Para clasificar muchas imagenes se llama a generate_json en paralelo.
"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional

try:  # el SDK solo hace falta en runtime
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - solo si falta el paquete
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]


class GeminiVertex:
    """Cliente fino de Gemini en Vertex (sin claves: usa la SA). Sin logica de job."""

    def __init__(self, project: str, location: str, model: str, client=None, max_retries: int = 3,
                 media_resolution: str = "LOW"):
        self.project = project
        self.location = location
        self.model = model
        self.max_retries = max(1, max_retries)
        self._media_resolution = self._resolve_media(media_resolution)
        if client is None:
            if genai is None:
                raise RuntimeError("google-genai no esta instalado; instala las dependencias.")
            client = genai.Client(vertexai=True, project=project, location=location)
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
        )

    def generate_json(self, image_bytes: bytes, prompt: str, schema: Optional[Dict] = None,
                      mime_type: str = "image/jpeg") -> Dict:
        """Una imagen -> JSON parseado (dict). Reintenta transitorios (429/503/timeout/JSON)."""
        contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt]
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.models.generate_content(
                    model=self.model, contents=contents, config=self._config(schema)
                )
                return json.loads(resp.text)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
        raise last_exc  # type: ignore[misc]
