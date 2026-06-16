"""Orquestador del job ingest-public.

Flujo:
  1. Lee --dataset <nombre>.
  2. Carga la config tipada (libs.config).
  3. Descarga _descriptors/<dataset>.yml de raw-public y lo parsea.
  4. Resuelve el adaptador segun el campo `adapter:` (registry).
  5. Por cada muestra: copia la imagen a BKT_PUBLIC y escribe su .lines.json al lado.
  6. Acumula filas de tbl_images (upsert por image_id) y escribe el <split>.txt.
  7. Escribe una fila en tbl_source_datasets (num_images contado, prefijo, fecha).
  8. Aplica options (overwrite, skip_missing_labels), loguea y sale 0/!=0.

Uso:
  python -m src.main --dataset culane
  python -m src.main --dataset culane --dry-run   # no escribe nada (solo loguea)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import yaml

from libs.bigquery import BigQueryWriter
from libs.config import Settings, load_settings
from libs.gcs import GcsClient

from .adapters.base import (
    format_label_json,
    image_out_path,
    label_out_path,
    to_label_json,
    txt_out_path,
)
from .registry import get_adapter

log = logging.getLogger("ingest-public")


def _epoch_to_bq(epoch: int) -> str:
    """Epoch -> literal TIMESTAMP de BigQuery (UTC)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _notes(descriptor: Dict) -> str | None:
    parts = [descriptor.get("source_url"), descriptor.get("notes")]
    text = " | ".join(p for p in parts if p)
    return text or None


def build_image_row(
    *,
    image_id: str,
    dataset: str,
    gcs_uri: str,
    split: str,
    ingested_at: str,
    width: int | None = None,
    height: int | None = None,
    frame_id: str | None = None,
    sequence_id: str | None = None,
) -> Dict:
    """Fila para tbl_images (source siempre 'public' en ingest)."""
    return {
        "image_id": image_id,
        "source": "public",
        "dataset": dataset,
        "gcs_uri": gcs_uri,
        "width": width,
        "height": height,
        "frame_id": frame_id,
        "sequence_id": sequence_id,
        "user_id": None,
        "captured_at": None,
        "ingested_at": ingested_at,
        "split": split,
    }


def build_source_dataset_row(
    *,
    dataset: str,
    version: str,
    license_: str | None,
    num_images: int,
    gcs_prefix: str,
    normalized_at: str,
    notes: str | None = None,
) -> Dict:
    """Fila para tbl_source_datasets."""
    return {
        "dataset": dataset,
        "version": version,
        "license": license_,
        "num_images": num_images,
        "gcs_prefix": gcs_prefix,
        "normalized_at": normalized_at,
        "notes": notes,
    }


def run(
    dataset: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    settings: Settings | None = None,
    gcs: GcsClient | None = None,
    bq: BigQueryWriter | None = None,
) -> Dict:
    """Ejecuta la ingesta+normalizacion de un dataset. Devuelve un resumen.

    `gcs` y `bq` se pueden inyectar (tests); por defecto usan los clientes reales.
    `limit` acota el numero de muestras procesadas (smoke test de solo lectura
    junto con `dry_run=True`).
    """
    settings = settings or load_settings()
    gcs = gcs or GcsClient()

    descriptor_uri = settings.descriptor_uri(dataset)
    log.info("Reading descriptor %s", descriptor_uri)
    descriptor = yaml.safe_load(gcs.read_text(descriptor_uri)) or {}

    adapter_name = descriptor.get("adapter")
    if not adapter_name:
        raise ValueError(f"descriptor for {dataset!r} does not define 'adapter'")
    adapter = get_adapter(adapter_name)(descriptor, gcs, settings.raw_public_uri(dataset))

    options = descriptor.get("options") or {}
    overwrite = bool(options.get("overwrite", False))
    skip_missing = bool(options.get("skip_missing_labels", True))
    ingest_unlabeled = bool(options.get("ingest_unlabeled", False))

    epoch = int(time.time())
    ingested_at = _epoch_to_bq(epoch)
    out_prefix = settings.public_uri(dataset)

    txt_groups: Dict[str, List[str]] = defaultdict(list)
    image_rows: List[Dict] = []
    written = skipped_no_label = reused = processed = unlabeled = 0

    for sample in adapter.iter_samples():
        if limit is not None and processed >= limit:
            break
        processed += 1
        if not sample.has_label and not ingest_unlabeled:
            if skip_missing:
                skipped_no_label += 1
                log.debug("missing label, skipping: %s", sample.rel_path)
                continue
            raise RuntimeError(f"image without label: {sample.rel_path}")
        if not sample.has_label:
            unlabeled += 1

        img_rel = image_out_path(sample)
        img_uri = f"{out_prefix}/{img_rel}"
        lbl_uri = f"{out_prefix}/{label_out_path(sample)}"

        if not dry_run:
            if not overwrite and gcs.exists(img_uri):
                reused += 1
            else:
                gcs.copy(sample.src_image_uri, img_uri)
                if sample.has_label:
                    label_bytes = format_label_json(to_label_json(sample.lanes, epoch)).encode("utf-8")
                    gcs.upload_bytes(lbl_uri, label_bytes, "application/json")

        txt_groups[txt_out_path(sample)].append(img_rel)
        image_rows.append(
            build_image_row(
                image_id=sample.image_id,
                dataset=sample.dataset,
                gcs_uri=img_uri,
                split=sample.split,
                ingested_at=ingested_at,
                width=sample.width,
                height=sample.height,
                frame_id=sample.frame_id,
                sequence_id=sample.sequence_id,
            )
        )
        written += 1

    # --- ficheros <split>.txt (una ruta de imagen por linea) ---
    for txt_rel, lines in txt_groups.items():
        body = "\n".join(sorted(set(lines))) + "\n"
        if not dry_run:
            gcs.upload_bytes(f"{out_prefix}/{txt_rel}", body.encode("utf-8"), "text/plain")

    num_images = len(image_rows)
    log.info(
        "Samples: %d normalized (%d reused, %d unlabeled, %d skipped without labels)",
        num_images, reused, unlabeled, skipped_no_label,
    )

    # --- BigQuery: upsert tbl_images + fila en tbl_source_datasets ---
    if not dry_run and image_rows:
        writer = bq or BigQueryWriter(location=settings.bq_location)
        writer.upsert(settings.tbl_images, image_rows, ["image_id"])
        source_row = build_source_dataset_row(
            dataset=dataset,
            version=str(descriptor.get("version") or ""),
            license_=descriptor.get("license") or None,
            num_images=num_images,
            gcs_prefix=out_prefix,
            normalized_at=ingested_at,
            notes=_notes(descriptor),
        )
        writer.upsert(settings.tbl_source_datasets, [source_row], ["dataset", "version"])
        log.info("BigQuery updated: %s (+1 in tbl_source_datasets)", settings.tbl_images)

    return {
        "dataset": dataset,
        "adapter": adapter_name,
        "num_images": num_images,
        "reused": reused,
        "unlabeled": unlabeled,
        "skipped_no_label": skipped_no_label,
        "splits": sorted({row["split"] for row in image_rows}),
        "dry_run": dry_run,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest and normalize a public dataset.")
    parser.add_argument("--dataset", required=True, help="short dataset name (folder in raw-public)")
    parser.add_argument("--dry-run", action="store_true", help="do not write to GCS or BigQuery")
    parser.add_argument("--limit", type=int, default=None, help="process at most N samples (smoke test)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run(args.dataset, dry_run=args.dry_run, limit=args.limit)
    except Exception:  # noqa: BLE001 - el job debe salir !=0 ante cualquier fallo
        log.exception("Ingestion failed for %s", args.dataset)
        return 1
    log.info("OK %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
