"""Orquestador del job classify.

Flujo:
  1. Lee --source public|user (y opcional --dataset).
  2. Pregunta a BigQuery que imagenes de ese source/dataset NO estan aun en
     tbl_classifications (anti-join contra tbl_images).
  3. Por cada imagen (en paralelo): la lee de GCS (su gcs_uri) y la clasifica con
     Gemini en Vertex (libs.vertex) -> {weather, scene, timeofday, road_geometry}.
  4. Upsert en tbl_classifications (MERGE por image_id, libs.bigquery).

Uso:
  python -m src.main --source public --dataset culane
  python -m src.main --source user
  python -m src.main --source public --dataset culane --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

from libs.bigquery import BigQueryWriter
from libs.config import Settings, load_settings
from libs.gcs import GcsClient
from libs.parallel import imap_unordered

from .classifier import SceneClassifier

log = logging.getLogger("classify")


def _unclassified_sql(settings: Settings, dataset: Optional[str]) -> str:
    """SELECT de imagenes del source (y dataset) que aun no tienen clasificacion."""
    where = "i.source = @source AND c.image_id IS NULL"
    if dataset:
        where += " AND i.dataset = @dataset"
    return (
        f"SELECT i.image_id AS image_id, i.gcs_uri AS gcs_uri\n"
        f"FROM `{settings.tbl_images}` i\n"
        f"LEFT JOIN `{settings.tbl_classifications}` c ON i.image_id = c.image_id\n"
        f"WHERE {where}"
    )


def _row(target: Dict, labels: Dict, model: str, classified_at) -> Dict:
    """Fila de tbl_classifications a partir de las etiquetas de Gemini."""
    return {
        "image_id": target["image_id"],
        "weather": labels["weather"],
        "scene": labels["scene"],
        "timeofday": labels["timeofday"],
        "road_geometry": labels["road_geometry"],
        "model": model,
        "scores": None,
        "classified_at": classified_at,
        "geometry_at": classified_at,
    }


def run(
    *,
    source: str,
    dataset: Optional[str] = None,
    dry_run: bool = False,
    limit: int | None = None,
    workers: int | None = None,
    batch: bool | None = None,
    settings: Settings | None = None,
    gcs: GcsClient | None = None,
    bq: BigQueryWriter | None = None,
    classifier: "SceneClassifier | None" = None,
) -> Dict:
    """Clasifica las imagenes pendientes de `source` (y `dataset`). Devuelve un resumen.

    `batch` (por defecto GEMINI_BATCH): True -> lote por gs:// URI (mas barato,
    asincrono, sin descargar); False -> llamada sincrona por imagen en paralelo.
    `gcs`/`bq`/`classifier` se pueden inyectar (tests). `dry_run` clasifica pero no
    escribe en BigQuery.
    """
    settings = settings or load_settings()
    gcs = gcs or GcsClient()
    bq = bq or BigQueryWriter(location=settings.bq_location)
    classifier = classifier or SceneClassifier.from_settings(settings)
    if workers is None:
        workers = int(os.environ.get("WORKERS", "16"))
    workers = max(1, workers)
    if batch is None:
        batch = settings.gemini_batch

    params = [("source", "STRING", source)]
    if dataset:
        params.append(("dataset", "STRING", dataset))
    sql = _unclassified_sql(settings, dataset)
    if limit is not None:
        sql += f"\nLIMIT {int(limit)}"  # acota en BigQuery; no trae todo para recortar luego
    targets = bq.query(sql, params)
    log.info("To classify: %d image(s) (source=%s dataset=%s, batch=%s)",
             len(targets), source, dataset or "*", batch)

    epoch = int(time.time())
    classified_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
    model = settings.gemini_model

    rows: List[Dict] = []
    failed = 0
    if batch:
        # Lote: clasifica por gs:// URI (sin descargar). Una sola tarea asincrona.
        results = classifier.classify_uris([t["gcs_uri"] for t in targets])
        for target, labels in zip(targets, results):
            if labels is None:
                failed += 1
            else:
                rows.append(_row(target, labels, model, classified_at))
    else:
        # Sincrono en paralelo: descarga la imagen y la clasifica.
        def process(target: Dict) -> Dict:
            try:
                labels = classifier.classify(gcs.read_bytes(target["gcs_uri"]))
                return {"status": "ok", "row": _row(target, labels, model, classified_at)}
            except Exception as e:  # noqa: BLE001 - transitorios ya reintentados en vertex/gcs
                return {"status": "error", "image_id": target["image_id"], "error": f"{type(e).__name__}: {e}"}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for res in imap_unordered(executor, process, iter(targets), workers * 4):
                if res["status"] == "ok":
                    rows.append(res["row"])
                else:
                    failed += 1
                    if failed <= 20:
                        log.warning("failed %s: %s", res.get("image_id"), res.get("error"))

    if not dry_run and rows:
        bq.upsert(settings.tbl_classifications, rows, ["image_id"])
        log.info("BigQuery updated: %s (+%d rows)", settings.tbl_classifications, len(rows))

    return {
        "source": source,
        "dataset": dataset,
        "candidates": len(targets),
        "classified": len(rows),
        "failed": failed,
        "dry_run": dry_run,
        "batch": batch,
        "workers": workers,
        "model": model,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify normalized images (weather/scene/timeofday/road geometry) with Gemini on Vertex.",
    )
    parser.add_argument("--source", required=True, choices=["public", "user"], help="image source to classify")
    parser.add_argument("--dataset", default=None, help="restrict to this dataset (typical with --source public)")
    parser.add_argument("--dry-run", action="store_true", help="classify but do not write to BigQuery")
    parser.add_argument("--limit", type=int, default=None, help="classify at most N images (smoke test)")
    parser.add_argument("--workers", type=int, default=None, help="parallel workers for sync mode (default: WORKERS env or 16)")
    parser.add_argument("--batch", action=argparse.BooleanOptionalAction, default=None,
                        help="batch mode on/off (--batch/--no-batch); default: GEMINI_BATCH env")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        summary = run(
            source=args.source, dataset=args.dataset,
            dry_run=args.dry_run, limit=args.limit, workers=args.workers, batch=args.batch,
        )
    except Exception:  # noqa: BLE001 - el job debe salir !=0 ante cualquier fallo
        log.exception("Classification failed (source=%s dataset=%s)", args.source, args.dataset)
        return 1
    log.info("OK %s", summary)
    return 1 if summary.get("failed") else 0  # imagenes fallidas -> salida !=0 (re-ejecutar resume)


if __name__ == "__main__":
    sys.exit(main())
