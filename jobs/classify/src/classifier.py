"""Taxonomia y clasificador de escena vial (logica ESPECIFICA del job classify).

El prompt y la taxonomia viven en `classification.yaml` (datos, no codigo); aqui se
cargan y se derivan el `response_schema` y la validacion. libs/vertex.py es un
cliente generico de Gemini, sin nada de esto. `SceneClassifier` une ambos.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

import yaml

from libs.config import Settings
from libs.vertex import GeminiVertex

_SPEC_PATH = os.path.join(os.path.dirname(__file__), "classification.yaml")


def _load_spec(path: str = _SPEC_PATH):
    """Carga classification.yaml -> (prompt resuelto, fields). `{campo}` en el prompt
    se sustituye por la lista de valores permitidos de ese campo."""
    with open(path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    fields: Dict[str, List[str]] = dict(spec["fields"])
    prompt = spec["prompt"].format(**fields)
    return prompt, fields


PROMPT, FIELDS = _load_spec()


def response_schema(fields: Optional[Dict[str, List[str]]] = None) -> Dict:
    """Esquema JSON (con enums) que ata la salida de Gemini a la taxonomia."""
    f = fields or FIELDS
    return {
        "type": "object",
        "properties": {name: {"type": "string", "enum": vals} for name, vals in f.items()},
        "required": list(f),
    }


def validate(data: Dict, fields: Optional[Dict[str, List[str]]] = None) -> Dict:
    """Normaliza a la taxonomia: valor ausente o fuera de rango -> 'unknown'."""
    f = fields or FIELDS
    return {name: (data.get(name) if data.get(name) in vals else "unknown") for name, vals in f.items()}


class SceneClassifier:
    """Clasifica escenas viales: aporta prompt+schema (del YAML) al cliente general."""

    def __init__(self, gemini: GeminiVertex, prompt: str = PROMPT, fields: Optional[Dict] = None):
        self._gemini = gemini
        self._prompt = prompt
        self._fields = fields or FIELDS

    @classmethod
    def from_settings(cls, settings: Settings, media_resolution: str = "HIGH") -> "SceneClassifier":
        # El job clasifica en HIGH (mejor deteccion de curvas/escena). El cliente
        # general libs.vertex sigue en LOW por defecto; aqui se sube a proposito.
        return cls(GeminiVertex(settings.project_id, settings.vertex_location, settings.gemini_model,
                                media_resolution=media_resolution))

    def classify(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict:
        """Una imagen (bytes) -> {weather, scene, timeofday, road_geometry} validados."""
        raw = self._gemini.generate_json(image_bytes, self._prompt, response_schema(self._fields), mime_type)
        return validate(raw, self._fields)
