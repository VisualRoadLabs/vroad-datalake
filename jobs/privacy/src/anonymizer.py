"""Anonimizador de caras/matriculas (logica ESPECIFICA del job privacy).

Detector YOLOv8 (ultralytics) en CPU que localiza caras y matriculas y las
difumina con Gaussian blur (irreversible). El modelo (.pt del repo MIT
`dashcam_anonymizer`) se hornea en la imagen durante el build; aqui se carga de una
ruta fija y se ejecuta por LOTES en CPU. Sin Gemini: el detector es local y barato.

`detection_kind` y `blur_regions` son funciones puras (testables sin modelo ni GPU).
Las clases (cara/matricula) se leen de `model.names` por substring; NO se hardcodean
indices (el .pt podria mapearlos al reves).
"""
from __future__ import annotations

import os
import sys
import types
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pesados: solo en runtime / imagen del job
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - solo si faltan los paquetes
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:
    from ultralytics import YOLO
except Exception:  # pragma: no cover - solo si falta el paquete
    YOLO = None  # type: ignore[assignment]

from libs.config import Settings

# Ruta donde el Dockerfile hornea el modelo (override con env PRIVACY_MODEL_PATH).
_DEFAULT_MODEL_PATH = "/app/models/dashcam-anon-yolov8-v1.pt"
_DEFAULT_CONF = 0.1     # dashcam_anonymizer: conf bajo a proposito (prioriza recall/privacidad)
_DEFAULT_IMGSZ = 960    # dashcam_anonymizer: img_width=960
_DEFAULT_BLUR = 31      # dashcam_anonymizer: blur_radius=31 (impar)


def _install_ultralytics_legacy_aliases() -> None:
    """Compatibilidad para .pt antiguos que picklean rutas `ultralytics.yolo.*`.

    Ultralytics moderno movio esos modulos a `ultralytics.*` / `ultralytics.models.yolo`.
    Su loader ya aliasa varios submodulos, pero algunos checkpoints necesitan que el
    paquete padre `ultralytics.yolo` exista durante el unpickle.
    """
    try:
        import ultralytics
        import ultralytics.data
        import ultralytics.models.yolo
        import ultralytics.utils
    except Exception:
        return

    yolo_pkg = sys.modules.get("ultralytics.yolo")
    if yolo_pkg is None:
        yolo_pkg = types.ModuleType("ultralytics.yolo")
        yolo_pkg.__path__ = []  # marca de paquete para importar submodulos legacy
        sys.modules["ultralytics.yolo"] = yolo_pkg
        setattr(ultralytics, "yolo", yolo_pkg)

    aliases = {
        "ultralytics.yolo.data": ultralytics.data,
        "ultralytics.yolo.utils": ultralytics.utils,
        "ultralytics.yolo.v8": ultralytics.models.yolo,
    }
    for name, module in aliases.items():
        sys.modules.setdefault(name, module)
        setattr(yolo_pkg, name.rsplit(".", 1)[-1], module)


def detection_kind(name: str) -> Optional[str]:
    """Nombre de clase del modelo -> 'face' | 'plate' | None (clase a ignorar)."""
    n = (name or "").lower()
    if "face" in n:
        return "face"
    if "plate" in n or "licen" in n:
        return "plate"
    return None


def blur_regions(img, regions: Sequence[Tuple[float, float, float, float]],
                 blur_radius: int = _DEFAULT_BLUR):
    """Difumina (Gaussian) cada caja (x1,y1,x2,y2) sobre una COPIA de la imagen BGR.

    Acota las cajas a los limites de la imagen y fuerza kernel impar. Pura (solo cv2).
    """
    if cv2 is None:
        raise RuntimeError("opencv (cv2) no esta instalado.")
    out = img.copy()
    h, w = out.shape[:2]
    k = blur_radius if blur_radius % 2 == 1 else blur_radius + 1
    for (x1, y1, x2, y2) in regions:
        x1i = max(0, int(round(x1)))
        y1i = max(0, int(round(y1)))
        x2i = min(w, int(round(x2)))
        y2i = min(h, int(round(y2)))
        if x2i <= x1i or y2i <= y1i:
            continue
        roi = out[y1i:y2i, x1i:x2i]
        if roi.size == 0:
            continue
        out[y1i:y2i, x1i:x2i] = cv2.GaussianBlur(roi, (k, k), 0)
    return out


class Anonymizer:
    """Detecta (YOLOv8 CPU) y difumina caras/matriculas por LOTES. Sin logica de GCS/BQ."""

    def __init__(self, model, model_version: str, conf: float = _DEFAULT_CONF,
                 imgsz: int = _DEFAULT_IMGSZ, blur_radius: int = _DEFAULT_BLUR):
        self._model = model
        self._names = dict(getattr(model, "names", {}) or {})
        self.model_version = model_version
        self._conf = conf
        self._imgsz = imgsz
        self._blur_radius = blur_radius

    @classmethod
    def from_path(cls, model_path: str = _DEFAULT_MODEL_PATH, **kw) -> "Anonymizer":
        if YOLO is None:
            raise RuntimeError("ultralytics no esta instalado; instala las dependencias.")
        _install_ultralytics_legacy_aliases()
        model = YOLO(model_path)  # carga en CPU por defecto; device='cpu' se pasa por llamada
        version = os.path.splitext(os.path.basename(model_path))[0]  # p.ej. dashcam-anon-yolov8-v1
        return cls(model, model_version=version, **kw)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Anonymizer":
        if settings.anon_method != "detector":
            raise RuntimeError(
                f"privacy solo implementa ANON_METHOD=detector (recibido: {settings.anon_method!r})."
            )
        return cls.from_path(os.environ.get("PRIVACY_MODEL_PATH", _DEFAULT_MODEL_PATH))

    def _decode(self, data: bytes):
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)  # None si no decodifica

    def _regions(self, res):
        """Result de ultralytics -> (cajas a difuminar, n_caras, n_matriculas)."""
        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return [], 0, 0
        xyxy = boxes.xyxy.cpu().numpy()           # coords absolutas (x1,y1,x2,y2)
        cls = boxes.cls.cpu().numpy().astype(int)
        regions: List[Tuple] = []
        faces = plates = 0
        for box, c in zip(xyxy, cls):
            kind = detection_kind(self._names.get(int(c), ""))
            if kind is None:
                continue
            regions.append(tuple(box))
            if kind == "face":
                faces += 1
            else:
                plates += 1
        return regions, faces, plates

    def anonymize_batch(self, images_bytes: Sequence[bytes]) -> List[Optional[Dict]]:
        """Lote de bytes JPEG -> lista alineada de dict|None.

        dict: {clean: bytes, faces: int, plates: int, width: int, height: int}.
        None si la imagen no decodifica o no se pudo recodificar.
        """
        if cv2 is None or np is None:
            raise RuntimeError("opencv/numpy no instalados.")
        decoded = [self._decode(b) for b in images_bytes]
        idx = [i for i, a in enumerate(decoded) if a is not None]
        out: List[Optional[Dict]] = [None] * len(images_bytes)
        if not idx:
            return out
        preds = self._model.predict(
            source=[decoded[i] for i in idx], device="cpu",
            conf=self._conf, imgsz=self._imgsz, verbose=False, save=False,
        )
        for i, res in zip(idx, preds):
            img = decoded[i]
            regions, faces, plates = self._regions(res)
            clean = blur_regions(img, regions, self._blur_radius) if regions else img
            ok, enc = cv2.imencode(".jpg", clean, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if not ok:
                continue
            h, w = img.shape[:2]
            out[i] = {"clean": enc.tobytes(), "faces": faces, "plates": plates,
                      "width": int(w), "height": int(h)}
        return out
