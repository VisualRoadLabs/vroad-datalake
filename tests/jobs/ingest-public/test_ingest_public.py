"""Smoke test de solo lectura del job ingest-public contra GCP real.

Ejecuta el job en dry-run (no escribe nada) sobre unas pocas muestras e imprime
el resumen (ejecutar con `pytest -s`). Elige el primer descriptor valido bajo
_descriptors/ (uno que define `adapter`); si no hay ninguno, salta el test.
"""
from __future__ import annotations

import os

import pytest
import yaml

from src.main import run


def _pick_dataset(cloud) -> str:
    forced = os.environ.get("TEST_DATASET")
    if forced:
        return forced
    s = cloud["settings"]
    prefix = s.descriptors_prefix.strip("/") + "/"
    for blob in cloud["storage"].list_blobs(s.bkt_raw_public, prefix=prefix, timeout=30):
        if not blob.name.endswith(".yml") or blob.name.endswith("default.yml"):
            continue
        try:
            descriptor = yaml.safe_load(blob.download_as_text()) or {}
        except Exception:  # noqa: BLE001 - ignora descriptores ilegibles
            continue
        if descriptor.get("adapter"):
            return blob.name.rsplit("/", 1)[-1][: -len(".yml")]
    pytest.skip("No valid <dataset>.yml (with 'adapter') under _descriptors/ (or set TEST_DATASET)")


def test_job_dry_run(cloud):
    dataset = _pick_dataset(cloud)
    summary = run(dataset, dry_run=True, limit=5, settings=cloud["settings"])
    print(f"\n[ingest] dry-run summary: {summary}")
    assert summary["dry_run"] is True
    assert summary["num_images"] <= 5
