"""Adaptador CULane.

Estructura nativa estandar (relativa a <dataset>/):
    driver_<id>_<n>frame/<clip>.MP4/<frame>.jpg
    driver_<id>_<n>frame/<clip>.MP4/<frame>.lines.txt   # "x1 y1 x2 y2 ..." por carril
    list/train.txt  list/val.txt  list/test.txt
    list/test_split/test0_normal.txt ... test8_night.txt

Las etiquetas .lines.txt: cada linea es un carril como pares 'x y' (a veces con
decimales -> se redondean al escribir el JSON). El test se entrega dividido por
categorias y se respeta (test/<categoria>/...).

CULane se redistribuye de varias formas (listas en `list/` o `list/list/`,
etiquetas junto a la imagen o en `annotations_new/`, imagenes anidadas bajo su
carpeta de driver). El adaptador AUTODETECTA el layout una vez, o se fuerza por
`options` en el descriptor:
    options:
      list_dir:  list/list        # carpeta de las listas
      label_dir: annotations_new  # carpeta de los .lines.txt
El `rel_path` de salida es siempre el estandar limpio: la salida normalizada no
hereda el anidamiento del crudo. Resolucion fija 1640x590.
"""
from __future__ import annotations

import posixpath
from typing import Iterator, List, Optional

from .base import BaseAdapter, Lane, Sample


def parse_lines_txt(text: str) -> List[Lane]:
    """Parsea un .lines.txt de CULane en carriles de puntos (x, y)."""
    lanes: List[Lane] = []
    for line in text.splitlines():
        nums = line.split()
        if len(nums) < 4:
            continue
        coords = [float(n) for n in nums]
        points = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        if points:
            lanes.append(points)
    return lanes


class CulaneAdapter(BaseAdapter):
    name = "culane"
    width = 1640
    height = 590

    def iter_samples(self, splits: set[str] | None = None) -> Iterator[Sample]:
        list_dir = self._resolve_list_dir()
        self._detect_file_layout(list_dir)

        if splits is None or "train" in splits:
            train = self._join(list_dir, "train.txt")
            if self.store.exists(train):
                yield from self._samples_from(train, "train", None)

        if splits is None or "val" in splits:
            val = self._join(list_dir, "val.txt")
            if self.store.exists(val):
                yield from self._samples_from(val, "val", None)

        if splits is None or "test" in splits:
            cat_files = sorted(
                p for p in self._safe_list(self._join(list_dir, "test_split")) if p.endswith(".txt")
            )
            if cat_files:
                for path in cat_files:
                    yield from self._samples_from(path, "test", self._category_from(path))
            else:
                test = self._join(list_dir, "test.txt")
                if self.store.exists(test):
                    yield from self._samples_from(test, "test", None)

    # --- helpers ---
    def _opt(self, key: str):
        return (self.descriptor.get("options") or {}).get(key)

    @staticmethod
    def _category_from(path: str) -> str:
        stem = posixpath.splitext(posixpath.basename(path))[0]  # test0_normal
        _, _, cat = stem.partition("_")
        return cat or stem

    def _resolve_list_dir(self) -> str:
        forced = self._opt("list_dir")
        if forced:
            return forced
        for cand in ("list", "list/list"):
            if self.store.exists(self._join(cand, "train.txt")) or self._safe_list(
                self._join(cand, "test_split")
            ):
                return cand
        return "list"

    def _first_entry(self, list_dir: str) -> Optional[str]:
        candidates = [self._join(list_dir, n) for n in ("train.txt", "val.txt", "test.txt")]
        candidates += [
            p for p in self._safe_list(self._join(list_dir, "test_split")) if p.endswith(".txt")
        ]
        for path in candidates:
            if not self.store.exists(path):
                continue
            for line in self.store.read_text(path).splitlines():
                entry = line.strip()
                if entry:
                    return entry.split()[0].lstrip("/")
        return None

    def _detect_file_layout(self, list_dir: str) -> None:
        """Detecta una vez donde viven imagenes y etiquetas en este crudo."""
        self._img_under_driver = False
        self._label_strategy = "alongside"  # alongside | nested | annotations_new | <carpeta>

        rel = self._first_entry(list_dir)
        if rel is None:
            return
        driver = rel.split("/", 1)[0]
        stem = posixpath.splitext(rel)[0] + ".lines.txt"

        # imagen: estandar (<rel>) o anidada bajo su driver (<driver>/<rel>)
        if self.store.exists(self._join(rel)):
            self._img_under_driver = False
        elif self.store.exists(self._join(driver, rel)):
            self._img_under_driver = True

        # etiqueta: forzada por descriptor, o autodetectada
        forced = self._opt("label_dir")
        if forced:
            self._label_strategy = forced
            return
        for strategy, path in (
            ("alongside", stem),
            ("nested", f"{driver}/{stem}"),
            ("annotations_new", f"annotations_new/{stem}"),
        ):
            if self.store.exists(self._join(path)):
                self._label_strategy = strategy
                break

    def _image_uri(self, rel: str) -> str:
        if getattr(self, "_img_under_driver", False):
            return self._join(rel.split("/", 1)[0], rel)
        return self._join(rel)

    def _label_path(self, rel: str) -> str:
        stem = posixpath.splitext(rel)[0] + ".lines.txt"
        strategy = getattr(self, "_label_strategy", "alongside")
        if strategy == "alongside":
            return self._join(stem)
        if strategy == "nested":
            return self._join(rel.split("/", 1)[0], stem)
        # "annotations_new" o cualquier carpeta forzada por label_dir
        return self._join(strategy, stem)

    def _parse(self, text: str) -> List[Lane]:
        return parse_lines_txt(text)

    def _samples_from(self, list_path: str, split: str, category: Optional[str]) -> Iterator[Sample]:
        for line in self.store.read_text(list_path).splitlines():
            entry = line.strip()
            if not entry:
                continue
            # En *_gt.txt hay varias columnas; la imagen es la primera.
            rel = entry.split()[0].lstrip("/")
            yield Sample(
                dataset=self.dataset,
                split=split,
                rel_path=rel,
                src_image_uri=self._image_uri(rel),
                category=category,
                width=self.width,
                height=self.height,
                label_uri=self._label_path(rel),
            )
