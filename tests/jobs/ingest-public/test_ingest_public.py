"""Read-only smoke test of the ingest-public job against real GCP.

Runs the job in dry-run (writes nothing) over a few samples and prints the
summary (run with `pytest -s`). Picks the first valid descriptor under
_descriptors/ (one that defines `adapter`); skips if there is none.
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
