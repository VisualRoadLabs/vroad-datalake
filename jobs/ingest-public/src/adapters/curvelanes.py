"""Adaptador CurveLanes.

Estructura nativa (ya muy cercana al formato de salida):
    train/images/<...>.jpg   train/labels/<...>.lines.json   train/train.txt
    valid/images/...         valid/labels/...                valid/valid.txt
    test/images/...   (test: solo imagenes; sin labels ni test.txt -> ver ingest_unlabeled)

Mapeo de splits: valid -> val (el resto igual). El .txt nativo lista rutas
`images/<...>.jpg`; se les quita el prefijo `images/` para no duplicarlo en el
layout de salida. Las etiquetas ya son JSON {"Lines": [[{"x","y"}, ...], ...]}
con coordenadas decimales en string -> se redondean a entero al escribir.
"""
from __future__ import annotations

import json
import posixpath
from typing import Iterator, List

from .base import BaseAdapter, Lane, Sample


def parse_curvelanes_json(text: str) -> List[Lane]:
    """Parsea un .lines.json de CurveLanes en carriles de puntos (x, y)."""
    data = json.loads(text)
    lanes: List[Lane] = []
    for lane in data.get("Lines", []):
        points = [(float(p["x"]), float(p["y"])) for p in lane]
        if points:
            lanes.append(points)
    return lanes


class CurvelanesAdapter(BaseAdapter):
    name = "curvelanes"
    # Resolucion variable -> width/height quedan en None (NULLABLE en BQ).

    # (carpeta nativa, split canonico)
    NATIVE_SPLITS = (("train", "train"), ("valid", "val"), ("test", "test"))

    def iter_samples(self, splits: set[str] | None = None) -> Iterator[Sample]:
        ingest_unlabeled = bool((self.descriptor.get("options") or {}).get("ingest_unlabeled", False))
        for native, split in self.NATIVE_SPLITS:
            if splits is not None and split not in splits:
                continue
            for native_img in self._entries(native, ingest_unlabeled):
                yield Sample(
                    dataset=self.dataset,
                    split=split,
                    rel_path=self._strip_images(native_img),      # a/1.jpg
                    src_image_uri=self._join(native, native_img),
                    width=self.width,
                    height=self.height,
                    label_uri=self._join(native, self._label_native(native_img)),
                )

    def _parse(self, text: str) -> List[Lane]:
        return parse_curvelanes_json(text)

    def _entries(self, native: str, ingest_unlabeled: bool) -> List[str]:
        """Rutas nativas de imagen (`images/<...>`) de un split.

        Vienen de `<split>.txt`; si no existe y `ingest_unlabeled`, se descubren
        listando `images/` (caso CurveLanes test: solo imagenes, sin lista ni GT).
        """
        list_path = self._join(native, f"{native}.txt")
        if self.store.exists(list_path):
            return [
                line.strip().lstrip("/")
                for line in self.store.read_text(list_path).splitlines()
                if line.strip()
            ]
        if ingest_unlabeled:
            root = self._join(native) + "/"
            return [
                uri[len(root):]
                for uri in self._safe_list(self._join(native, "images"))
                if uri.endswith(self.image_ext) and uri.startswith(root)
            ]
        return []

    @staticmethod
    def _strip_images(path: str) -> str:
        return path[len("images/"):] if path.startswith("images/") else path

    def _label_native(self, native_img: str) -> str:
        base = self._strip_images(native_img)
        stem = posixpath.splitext(base)[0]
        return f"labels/{stem}.lines.json"
