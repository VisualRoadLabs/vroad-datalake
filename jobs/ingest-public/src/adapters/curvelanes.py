"""Adaptador CurveLanes.

Estructura nativa (ya muy cercana al formato de salida):
    train/images/<...>.jpg   train/labels/<...>.lines.json   train/train.txt
    valid/images/...         valid/labels/...                valid/valid.txt
    test/images/...                                          test/test.txt   (sin labels)

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

    def iter_samples(self) -> Iterator[Sample]:
        for native, split in self.NATIVE_SPLITS:
            list_path = self._join(native, f"{native}.txt")
            if not self.store.exists(list_path):
                continue
            for line in self.store.read_text(list_path).splitlines():
                entry = line.strip()
                if not entry:
                    continue
                native_img = entry.lstrip("/")           # images/a/1.jpg
                rel = self._strip_images(native_img)      # a/1.jpg
                label_path = self._join(native, self._label_native(native_img))
                lanes: List[Lane] = []
                has_label = self.store.exists(label_path)
                if has_label:
                    lanes = parse_curvelanes_json(self.store.read_text(label_path))
                yield Sample(
                    dataset=self.dataset,
                    split=split,
                    rel_path=rel,
                    src_image_uri=self._join(native, native_img),
                    lanes=lanes,
                    has_label=has_label,
                    width=self.width,
                    height=self.height,
                    label_uri=label_path if has_label else None,
                )

    @staticmethod
    def _strip_images(path: str) -> str:
        return path[len("images/"):] if path.startswith("images/") else path

    def _label_native(self, native_img: str) -> str:
        base = self._strip_images(native_img)
        stem = posixpath.splitext(base)[0]
        return f"labels/{stem}.lines.json"
