"""Smoke test del job classify contra GCP real (Gemini en Vertex), como en ingest.

Lee imagenes reales del bucket publico y las clasifica con Gemini de verdad; el
dry-run NO escribe en BigQuery. Imprime el resultado (ejecutar con `pytest -s`).
Usa el fixture `cloud` -> se salta si GCP no esta accesible (sin ADC/red). Las
llamadas a Gemini van a media_resolution LOW (baratas).
"""
from __future__ import annotations

import pytest

from src.classifier import SceneClassifier
from src.main import run

_IMG_EXTS = (".jpg", ".jpeg", ".png")


def _first_image(cloud) -> str:
    """Primer objeto-imagen del bucket publico (o salta si no hay ninguno)."""
    settings = cloud["settings"]
    for blob in cloud["storage"].list_blobs(settings.bkt_public, max_results=200, timeout=30):
        if blob.name.lower().endswith(_IMG_EXTS):
            return blob.name
    pytest.skip(f"{settings.bkt_public} sin imagenes (ejecuta un ingest real primero)")


def test_classify_one_real_image(cloud):
    settings = cloud["settings"]
    name = _first_image(cloud)
    image = cloud["storage"].bucket(settings.bkt_public).blob(name).download_as_bytes()
    labels = SceneClassifier.from_settings(settings).classify(image)
    print(f"\n[classify] {name} -> {labels}")
    assert set(labels) == {"weather", "scene", "timeofday", "road_geometry"}
    assert all(isinstance(v, str) and v for v in labels.values())


def test_run_dry_run_no_writes(cloud):
    settings = cloud["settings"]
    try:
        from libs.bigquery import BigQueryWriter

        bq = BigQueryWriter(location=settings.bq_location)
    except Exception as e:  # noqa: BLE001 - paquete bigquery ausente -> no es fallo del job
        pytest.skip(f"BigQuery no disponible ({type(e).__name__}: {str(e)[:120]})")
    summary = run(source="public", dry_run=True, limit=2, batch=False, settings=settings, bq=bq)
    print(f"\n[classify] dry-run summary: {summary}")
    assert summary["dry_run"] is True and summary["batch"] is False
    assert summary["candidates"] <= 2
    assert summary["classified"] <= summary["candidates"]
