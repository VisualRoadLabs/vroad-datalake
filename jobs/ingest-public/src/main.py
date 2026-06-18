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
import itertools
import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List

import yaml

from libs.bigquery import BigQueryWriter
from libs.config import Settings, load_settings
from libs.gcs import GcsClient
from libs.parallel import imap_unordered as _imap_unordered

from .adapters.base import (
    format_label_json,
    image_out_path,
    label_out_path,
    to_label_json,
    txt_out_path,
)
from .registry import get_adapter

log = logging.getLogger("ingest-public")


def _epoch_to_bq(epoch: int) -> datetime:
    """Epoch -> datetime UTC (valor para parametros TIMESTAMP de BigQuery)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


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
    workers: int | None = None,
    settings: Settings | None = None,
    gcs: GcsClient | None = None,
    bq: BigQueryWriter | None = None,
) -> Dict:
    """Ejecuta la ingesta+normalizacion de un dataset. Devuelve un resumen.

    Procesa las imagenes en paralelo (casi todo el tiempo es esperar a GCS):
    `workers` hilos (por defecto la env `INGEST_WORKERS` o 16). `gcs`/`bq` se pueden
    inyectar (tests). `limit` acota las muestras (smoke test con `dry_run=True`).
    """
    settings = settings or load_settings()
    gcs = gcs or GcsClient()
    if workers is None:
        workers = int(os.environ.get("INGEST_WORKERS", "16"))
    workers = max(1, workers)

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

    def process(sample) -> Dict:
        """Worker (en paralelo): lee la etiqueta, copia la imagen y sube el .json.

        No muta estado compartido y NUNCA lanza: devuelve el resultado (o un error
        por imagen) para que el hilo principal agregue de forma segura.

        Contrato de hilos: los workers solo llaman `adapter.read_label(label_uri)` y
        operaciones de GcsClient (que crean bucket/blob frescos por llamada sobre un
        storage.Client thread-safe). NO leen estado del adapter resuelto en el hilo
        principal (p. ej. CULane._label_strategy/_img_under_driver): eso ya viene
        cocinado en Sample.label_uri / Sample.src_image_uri.
        """
        try:
            lanes = adapter.read_label(sample.label_uri)
            has_label = lanes is not None
            if not has_label and not ingest_unlabeled:
                if skip_missing:
                    return {"status": "skipped"}
                return {"status": "error", "rel": sample.rel_path, "error": "image without label"}

            img_rel = image_out_path(sample)
            img_uri = f"{out_prefix}/{img_rel}"
            reused = False
            if not dry_run:
                if not overwrite and gcs.exists(img_uri):
                    reused = True
                else:
                    gcs.copy(sample.src_image_uri, img_uri)
                    if has_label:
                        body = format_label_json(to_label_json(lanes, epoch)).encode("utf-8")
                        gcs.upload_bytes(f"{out_prefix}/{label_out_path(sample)}", body, "application/json")

            row = build_image_row(
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
            return {
                "status": "ok",
                "row": row,
                "img_rel": img_rel,
                "txt_rel": txt_out_path(sample),
                "reused": reused,
                "unlabeled": not has_label,
            }
        except Exception as e:  # noqa: BLE001 - los transitorios ya se reintentan en gcs
            return {"status": "error", "rel": sample.rel_path, "error": f"{type(e).__name__}: {e}"}

    sample_iter = adapter.iter_samples()
    if limit is not None:
        sample_iter = itertools.islice(sample_iter, limit)

    txt_groups: Dict[str, List[str]] = defaultdict(list)
    image_rows: List[Dict] = []
    reused = unlabeled = skipped = failed = 0
    processed = 0
    aborted = False

    # La agregacion ocurre solo en el hilo principal -> sin condiciones de carrera.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        try:
            for res in _imap_unordered(executor, process, sample_iter, workers * 4):
                processed += 1
                status = res["status"]
                if status == "skipped":
                    skipped += 1
                elif status == "error":
                    failed += 1
                    if failed <= 20:
                        log.warning("failed %s: %s", res.get("rel"), res.get("error"))
                else:
                    image_rows.append(res["row"])
                    txt_groups[res["txt_rel"]].append(res["img_rel"])
                    if res["reused"]:
                        reused += 1
                    if res["unlabeled"]:
                        unlabeled += 1
                if processed == 1:
                    # Comprobacion temprana: la primera muestra se proceso bien.
                    if status == "ok":
                        kind = "unlabeled" if res["unlabeled"] else ("reused" if res["reused"] else "new")
                        print(f"[ingest] first sample OK: {res['row']['image_id']} -> {res['img_rel']} ({kind})", flush=True)
                    elif status == "skipped":
                        print("[ingest] first sample: skipped (image without label)", flush=True)
                    else:
                        print(f"[ingest] first sample FAILED: {res.get('rel')}: {res.get('error')}", flush=True)
                if processed % 100 == 0:  # resumen periodico del avance
                    log.info("Progress: %d done (%d normalized, %d reused, %d unlabeled, %d skipped, %d failed)",
                             processed, len(image_rows), reused, unlabeled, skipped, failed)
        except Exception as e:  # noqa: BLE001 - fallo al GENERAR muestras (lectura de listas)
            # _imap_unordered ya drena y agrega lo completado antes de relanzar; aqui
            # registramos ese progreso parcial (txt + BQ) y salimos !=0 -> re-ejecutar resume.
            aborted = True
            log.error("sample generation aborted; recording partial progress: %s", e)

    # --- ficheros <split>.txt (una ruta de imagen por linea) ---
    # Sin overwrite se fusiona con el .txt previo (el listado solo crece). Solo se
    # (sobre)escribe si el contenido cambia: asi un re-run sin novedades no reescribe
    # nada (y no necesita permiso de borrado, ya que sobrescribir en GCS = borrar+crear).
    for txt_rel, lines in txt_groups.items():
        if dry_run:
            continue
        txt_uri = f"{out_prefix}/{txt_rel}"
        paths = set(lines)
        existing = None
        if not overwrite:
            try:
                existing = gcs.read_text(txt_uri)
                paths.update(p for p in existing.splitlines() if p)
            except Exception:  # noqa: BLE001 - aun no existia
                existing = None
        body = "\n".join(sorted(paths)) + "\n"
        if body != existing:
            gcs.upload_bytes(txt_uri, body.encode("utf-8"), "text/plain")

    num_images = len(image_rows)
    written = num_images - reused
    log.info(
        "Samples: %d normalized (%d new, %d reused, %d unlabeled, %d skipped, %d failed); workers=%d",
        num_images, written, reused, unlabeled, skipped, failed, workers,
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
        "written": written,
        "reused": reused,
        "unlabeled": unlabeled,
        "skipped_no_label": skipped,
        "failed": failed,
        "aborted": aborted,
        "splits": sorted({row["split"] for row in image_rows}),
        "dry_run": dry_run,
        "workers": workers,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest and normalize a public dataset.")
    parser.add_argument("--dataset", required=True, help="short dataset name (folder in raw-public)")
    parser.add_argument("--dry-run", action="store_true", help="do not write to GCS or BigQuery")
    parser.add_argument("--limit", type=int, default=None, help="process at most N samples (smoke test)")
    parser.add_argument("--workers", type=int, default=None, help="parallel workers (default: INGEST_WORKERS env or 16)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run(args.dataset, dry_run=args.dry_run, limit=args.limit, workers=args.workers)
    except Exception:  # noqa: BLE001 - el job debe salir !=0 ante cualquier fallo
        log.exception("Ingestion failed for %s", args.dataset)
        return 1
    log.info("OK %s", summary)
    # imagenes fallidas o generacion abortada -> salida !=0 (re-ejecutar resume, idempotente)
    return 1 if summary.get("failed") or summary.get("aborted") else 0


if __name__ == "__main__":
    sys.exit(main())
