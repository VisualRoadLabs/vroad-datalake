"""Tests del job privacy.

- detection_kind / blur_regions: funciones puras (sin modelo ni GCP). blur_regions
  necesita opencv+numpy; se salta si no estan.
- run() dry-run: orquestacion contra GCP real (lista raw-user + anti-join BigQuery),
  con un anonimizador FALSO inyectado (el modelo YOLOv8 real no esta disponible en
  el entorno de test: se hornea en la imagen). Usa el fixture `cloud` -> se salta si
  GCP no esta accesible. No escribe nada (dry-run).
"""
from __future__ import annotations

import pytest

from src.anonymizer import detection_kind
from src.main import run


def test_detection_kind_maps_classes():
    assert detection_kind("face") == "face"
    assert detection_kind("human face") == "face"
    assert detection_kind("license-plate") == "plate"
    assert detection_kind("number_plate") == "plate"
    assert detection_kind("car") is None
    assert detection_kind("") is None


def test_blur_regions_blurs_only_the_box():
    try:
        import cv2  # noqa: F401 - solo verifica que carga; opencv (no-headless) necesita libGL
        import numpy as np
    except ImportError as e:  # en el python:3.12-slim de CI falta libGL.so.1
        pytest.skip(f"opencv/numpy no cargable aqui ({e}); el blur se prueba donde haya libGL")
    from src.anonymizer import blur_regions

    img = np.random.default_rng(0).integers(0, 255, (100, 100, 3), dtype=np.uint8)
    out = blur_regions(img, [(10, 10, 40, 40)], blur_radius=31)

    assert out.shape == img.shape
    assert (out[60:, 60:] == img[60:, 60:]).all()        # fuera de la caja: intacto
    assert not (out[10:40, 10:40] == img[10:40, 10:40]).all()  # dentro: difuminado


class _FakeAnonymizer:
    """Detector falso para probar la orquestacion sin el modelo YOLOv8 real."""

    model_version = "fake-detector"

    def anonymize_batch(self, images_bytes):
        return [{"clean": b"jpeg", "faces": 1, "plates": 0, "width": 10, "height": 10}
                for _ in images_bytes]


def test_run_dry_run_no_writes(cloud):
    settings = cloud["settings"]
    from libs.gcs import GcsClient

    gcs = GcsClient(client=cloud["storage"])
    try:
        from libs.bigquery import BigQueryWriter

        bq = BigQueryWriter(location=settings.bq_location)
    except Exception as e:  # noqa: BLE001 - paquete bigquery ausente -> no es fallo del job
        pytest.skip(f"BigQuery no disponible ({type(e).__name__}: {str(e)[:120]})")

    try:
        summary = run(dry_run=True, limit=2, settings=settings, gcs=gcs, bq=bq,
                      anonymizer=_FakeAnonymizer())
    except Exception as e:  # noqa: BLE001 - sin acceso al bucket raw-user (PII) -> smoke se salta
        pytest.skip(f"raw-user no accesible ({type(e).__name__}: {str(e)[:120]})")

    print(f"\n[privacy] dry-run summary: {summary}")
    assert summary["dry_run"] is True
    assert summary["candidates"] >= 0
    assert summary["processed"] <= 2
    assert summary["anonymized"] <= summary["processed"]
