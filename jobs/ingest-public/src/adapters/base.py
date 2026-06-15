"""Contrato comun de los adaptadores de dataset publico.

main.py trata a todos los adaptadores igual: les pide `iter_samples()` y para
cada `Sample` escribe la imagen y su `.lines.json` en el layout normalizado.
El adaptador solo sabe leer su dataset nativo; el layout de salida y el formato
del JSON viven aqui (asi todos los datasets salen identicos).

LAYOUT DE SALIDA (relativo a gs://BKT_PUBLIC/<dataset>/):
    <split>/images/<ruta_nativa>.jpg
    <split>/label/<ruta_nativa>.lines.json
    <split>/<split>.txt                      (una ruta de imagen por linea)
donde <split> es train|val|test. Si la muestra tiene `category` (p. ej. las
categorias de test de CULane), se intercala como sub-carpeta:
    test/<category>/images/...  test/<category>/label/...  test/<category>/<category>.txt

FORMATO DEL JSON (estilo CurveLanes + timestamp epoch, enteros):
    {"timestamp": <epoch>, "Lines": [[{"x": int, "y": int}, ...], ...]}
"""
from __future__ import annotations

import json
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Protocol, Sequence, Tuple

# Un carril es una lista de puntos (x, y) en float (se redondean al escribir).
Lane = List[Tuple[float, float]]

# Splits canonicos del Data Lake (la carpeta de validacion es `val`, igual que
# el enum de tbl_images.split).
SPLITS = ("train", "val", "test")


class Store(Protocol):
    """Acceso a objetos que necesitan los adaptadores (GCS o local en tests)."""

    def read_text(self, path: str) -> str: ...
    def read_bytes(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def list(self, prefix: str) -> List[str]: ...


@dataclass
class Sample:
    """Una imagen normalizada lista para escribir."""

    dataset: str
    split: str                      # train | val | test
    rel_path: str                   # ruta nativa de la imagen, relativa al dataset
    src_image_uri: str              # de donde copiar la imagen (path del store)
    lanes: Lane = field(default_factory=list)  # carriles (vacio si no hay etiqueta)
    has_label: bool = True
    category: str | None = None     # sub-split (p. ej. CULane test 'normal')
    width: int | None = None
    height: int | None = None
    label_uri: str | None = None    # ruta de la etiqueta nativa (depurar/comparar)

    @property
    def image_id(self) -> str:
        return f"{self.dataset}/{posixpath.splitext(self.rel_path)[0]}"

    @property
    def frame_id(self) -> str:
        return posixpath.splitext(posixpath.basename(self.rel_path))[0]

    @property
    def sequence_id(self) -> str:
        return posixpath.dirname(self.rel_path)


# composicion de rutas de salida (relativas a gs://BKT_PUBLIC/<dataset>/)

def leaf_segments(sample: Sample) -> List[str]:
    """Segmentos de la 'hoja' de salida: [split] o [split, category]."""
    segs = [sample.split]
    if sample.category:
        segs.append(sample.category)
    return segs


def image_out_path(sample: Sample) -> str:
    return "/".join([*leaf_segments(sample), "images", sample.rel_path])


def label_out_path(sample: Sample) -> str:
    stem = posixpath.splitext(sample.rel_path)[0]
    return "/".join([*leaf_segments(sample), "label", f"{stem}.lines.json"])


def txt_out_path(sample: Sample) -> str:
    segs = leaf_segments(sample)
    return "/".join([*segs, f"{segs[-1]}.txt"])


def to_label_json(lanes: Sequence[Lane], timestamp: int) -> Dict:
    """Construye el JSON de etiquetas estilo CurveLanes con coords enteras."""
    return {
        "Lines": [
            [{"x": int(round(float(x))), "y": int(round(float(y)))} for x, y in lane]
            for lane in lanes
        ],
        "timestamp": int(timestamp),
    }


def format_label_json(label: Dict) -> str:
    """Serializa el GT como JSON legible: `timestamp` primero y UN carril por linea.

    Sigue siendo JSON valido; cada carril (lista de puntos) ocupa su propia linea,
    para poder distinguir las lineas de un vistazo. Asi sale en todos los datasets.
    """
    lane_rows = ",\n".join(json.dumps(lane) for lane in label["Lines"])
    return f'{{"timestamp": {int(label["timestamp"])},\n"Lines": [\n{lane_rows}\n]}}'


class BaseAdapter(ABC):
    """Clase base de todos los adaptadores.

    Subclases conocen la estructura nativa de su dataset y rinden `Sample`s.
    No escriben nada: solo leen (via `self.store`) y parsean.
    """

    name: str = "base"
    # Resolucion fija si el dataset la tiene (None = variable/desconocida).
    width: int | None = None
    height: int | None = None

    def __init__(self, descriptor: Dict, store: Store, raw_root: str):
        self.descriptor = descriptor or {}
        self.store = store
        self.raw_root = raw_root.rstrip("/")
        # nombre corto del dataset (= carpeta en raw-public)
        self.dataset = self.descriptor.get("dataset") or self.name
        # extension de imagen del descriptor (para derivar la ruta de etiqueta)
        self.image_ext = self.descriptor.get("image_ext") or ".jpg"

    def _join(self, *parts: str) -> str:
        """Une el root del dataset con sub-rutas (siempre con '/')."""
        clean = [p.strip("/") for p in parts if p not in (None, "")]
        return "/".join([self.raw_root, *clean])

    def _safe_list(self, prefix: str) -> List[str]:
        """list() que devuelve [] si el prefijo no existe o falla."""
        try:
            return self.store.list(prefix)
        except Exception:
            return []

    @abstractmethod
    def iter_samples(self) -> Iterator[Sample]:
        """Rinde una `Sample` por imagen del dataset."""
        raise NotImplementedError
