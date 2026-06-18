"""Cliente GENERAL de Gemini en Vertex AI (compartido por classify, privacy, ...).

NO contiene logica de ningun job: ni taxonomias, ni prompts, ni validacion. Cada
job aporta su prompt y su `response_schema` y valida su propia respuesta. Apunta a
Vertex con `vertexai=True` -> usa la SA del job automaticamente, SIN claves (ADC).

API:
  - generate_json(image_bytes, prompt, schema): llamada SINCRONA; devuelve el JSON
    ya parseado (dict). Reintenta transitorios.
  - batch_generate_json(image_uris, prompt, schema): prediccion en LOTE. Usa
    peticiones inline que referencian cada imagen por su gs:// URI
    (no descarga bytes ni escribe JSONL en GCS -> encaja con una SA read-only en
    GCS). Es ASINCRONA (hace polling). Devuelve una lista de dict|None alineada con
    `image_uris` (None si esa imagen fallo). El agente de servicio de Vertex debe
    poder leer las imagenes; si Vertex no admite lote inline, usar generate_json.
  - media_resolution (def. "LOW"): calidad de la imagen enviada a Gemini; LOW =
    menos tokens por imagen = mas barato. Se puede subir por instancia (p. ej.
    privacy podria querer HIGH para caras/matriculas pequenas).
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Sequence

try:  # el SDK solo hace falta en runtime
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - solo si falta el paquete
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

# Estados terminales de un batch job (JobState.<name>).
_TERMINAL = frozenset({
    "JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED", "JOB_STATE_PARTIALLY_SUCCEEDED",
})


def _state_name(job) -> str:
    return getattr(job.state, "name", str(job.state))


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

    def batch_generate_json(self, image_uris: Sequence[str], prompt: str, schema: Optional[Dict] = None,
                            mime_type: str = "image/jpeg", poll_seconds: int = 20,
                            max_wait_seconds: int = 3600) -> List[Optional[Dict]]:
        """Lote inline por gs:// URI -> lista de dict|None alineada con `image_uris`."""
        config = self._config(schema)
        requests = [
            types.InlinedRequest(
                model=self.model,
                contents=[types.Part.from_uri(file_uri=uri, mime_type=mime_type), prompt],
                config=config,
            )
            for uri in image_uris
        ]
        job = self._client.batches.create(model=self.model, src=requests)
        waited = 0
        while _state_name(job) not in _TERMINAL:
            if waited >= max_wait_seconds:
                raise TimeoutError(f"batch {job.name} no termino en {max_wait_seconds}s ({_state_name(job)})")
            time.sleep(poll_seconds)
            waited += poll_seconds
            job = self._client.batches.get(name=job.name)
        if _state_name(job) == "JOB_STATE_FAILED":
            raise RuntimeError(f"batch {job.name} fallo: {job.error}")

        out: List[Optional[Dict]] = []
        for resp in (job.dest.inlined_responses or []):
            if getattr(resp, "error", None) or getattr(resp, "response", None) is None:
                out.append(None)
                continue
            try:
                out.append(json.loads(resp.response.text))
            except Exception:  # noqa: BLE001 - respuesta no-JSON -> se marca como fallida
                out.append(None)
        return out
