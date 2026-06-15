"""Smoke tests de solo lectura para libs/ contra GCP real.

Imprime lo que lee (ejecutar con `pytest -s`). No escribe nada; el fixture
`cloud` salta los tests cloud si GCP no esta accesible.
"""
from __future__ import annotations

import pytest

from libs.config import load_settings


def test_config_loads():
    s = load_settings()
    print(f"\n[config] {s.project_id} | {s.bkt_public} | {s.tbl_images}")
    assert s.tbl_images.endswith(".tbl_images")
    assert s.descriptor_uri("culane").startswith("gs://")


def test_gcs_read_only(cloud):
    s = cloud["settings"]
    names = [b.name for b in cloud["storage"].list_blobs(s.bkt_raw_public, max_results=10, timeout=30)]
    print(f"\n[gcs] {s.bkt_raw_public}: {len(names)} objeto(s); muestra: {names[:5]}")
    assert isinstance(names, list)


def test_bigquery_read_only(cloud):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    bq = bigquery.Client(project=cloud["project"])
    datasets = [d.dataset_id for d in bq.list_datasets()]
    print(f"\n[bq] datasets in {cloud['project']}: {datasets}")
    assert isinstance(datasets, list)
