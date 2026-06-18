"""Orquestador del job privacy (anonimizacion de caras/matriculas de usuario).

Flujo (todo en CPU, por LOTES; sin Gemini):
  1. Lista el bucket crudo de usuario (bkt-prod-raw-user-usc1) -> imagenes images/*.jpg.
  2. Anti-join contra tbl_user_images_privacy (ya procesadas, ventana 48h) -> pendientes.
  3. Por LOTES: descarga (hilos), detecta+difumina con YOLOv8 (una llamada CPU por lote),
     sube la imagen limpia a bkt-prod-user-usc1 (misma ruta relativa) y copia lines.json si existe.
  4. MERGE en tbl_user_images_privacy (auditoria) y en tbl_images (catalogo source='user',
     para que classify recoja luego la imagen limpia). Checkpoint incremental.

El job NO borra el crudo (sin permiso de delete; lo borra el TTL 24h del bucket raw-user)
y NO usa KMS (GCS cifra/descifra el CMEK de forma transparente).

Uso:
  python -m src.main
  python -m src.main --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
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

from .anonymizer import Anonymizer

log = logging.getLogger("privacy")

_IMG_EXTS = (".jpg", ".jpeg", ".png")


def _image_id(raw_uri: str, bkt_raw_user: str) -> str:
    """gs://raw/<user>/<session>/images/<frame>.jpg -> user/<user>/<session>/images/<frame>."""
    rel = raw_uri[len(f"gs://{bkt_raw_user}/"):]
    return "user/" + os.path.splitext(rel)[0]


def _clean_uri(raw_uri: str, bkt_raw_user: str, bkt_user_clean: str) -> str:
    """Misma ruta relativa, solo cambia el bucket (raw -> clean)."""
    return raw_uri.replace(f"gs://{bkt_raw_user}/", f"gs://{bkt_user_clean}/", 1)


def _lines_uris(raw_uri: str, bkt_raw_user: str, bkt_user_clean: str):
    """(raw_lines_uri, clean_lines_uri): images/<frame>.jpg -> lines/<frame>.lines.json."""
    raw_lines = os.path.splitext(raw_uri.replace("/images/", "/lines/", 1))[0] + ".lines.json"
    clean_lines = raw_lines.replace(f"gs://{bkt_raw_user}/", f"gs://{bkt_user_clean}/", 1)
    return raw_lines, clean_lines


def _ids(rel: str):
    """<user>/<session>/images/<frame>.jpg -> (user_id, session_id, frame_id)."""
    head, _, tail = rel.partition("/images/")
    user_id, _, session_id = head.partition("/")
    frame_id = os.path.splitext(os.path.basename(tail))[0]
    return user_id, session_id, frame_id


def _parse_dt(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _session_dt(session_id: str) -> Optional[datetime]:
    """Datetime del prefijo del session_id (<fechahoraUTC>__...), p.ej. 20260314T0830Z."""
    prefix = session_id.split("__", 1)[0]
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%MZ"):
        try:
            return datetime.strptime(prefix, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _captured_at(gcs, bkt_raw_user, user_id, session_id, cache) -> Optional[datetime]:
    """captured_at de meta.json (cae al prefijo del session_id). Cacheado por sesion."""
    key = (user_id, session_id)
    if key in cache:
        return cache[key]
    ts = None
    try:
        meta = json.loads(gcs.read_text(f"gs://{bkt_raw_user}/{user_id}/{session_id}/meta.json"))
        for k in ("captured_at", "started_at", "start_time", "start", "date"):
            ts = _parse_dt(meta.get(k))
            if ts:
                break
    except Exception:  # noqa: BLE001 - meta.json puede no existir / no parsear
        ts = None
    ts = ts or _session_dt(session_id)
    cache[key] = ts
    return ts


def _audit_row(image_id, raw_uri, clean_uri, faces, plates, processed_at, model_version) -> Dict:
    """Fila de auditoria para tbl_user_images_privacy."""
    return {
        "image_id": image_id,
        "raw_gcs_uri": raw_uri,
        "clean_gcs_uri": clean_uri,
        "faces_blurred": faces,
        "plates_blurred": plates,
        "processed_at": processed_at,
        "model_version": model_version,
    }


def _catalog_row(image_id, clean_uri, res, user_id, session_id, frame_id, captured_at, ingested_at) -> Dict:
    """Fila de catalogo para tbl_images (source='user') -> classify la recogera luego."""
    return {
        "image_id": image_id,
        "source": "user",
        "dataset": "user",
        "gcs_uri": clean_uri,
        "width": res["width"],
        "height": res["height"],
        "frame_id": frame_id,
        "sequence_id": session_id,
        "user_id": user_id,
        "captured_at": captured_at,
        "ingested_at": ingested_at,
    }


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _recently_processed(bq, settings) -> set:
    """image_id ya anonimizados en las ultimas 48h (anti-join idempotente; raw vive 24h)."""
    sql = (
        f"SELECT image_id FROM `{settings.tbl_user_images_privacy}` "
        f"WHERE processed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)"
    )
    return {r["image_id"] for r in bq.query(sql)}


def run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    workers: int | None = None,
    batch_size: int | None = None,
    settings: Settings | None = None,
    gcs: GcsClient | None = None,
    bq: BigQueryWriter | None = None,
    anonymizer: "Anonymizer | None" = None,
) -> Dict:
    """Anonimiza las imagenes de usuario pendientes. Devuelve un resumen.

    Entrada = listado del bucket crudo (las imagenes crudas aun NO estan en tbl_images);
    idempotente via anti-join contra tbl_user_images_privacy. `dry_run` detecta+difumina
    pero no escribe en GCS ni BigQuery. `gcs`/`bq`/`anonymizer` se pueden inyectar (tests).
    """
    settings = settings or load_settings()
    gcs = gcs or GcsClient()
    bq = bq or BigQueryWriter(location=settings.bq_location)
    anonymizer = anonymizer or Anonymizer.from_settings(settings)
    if workers is None:
        workers = int(os.environ.get("PRIVACY_WORKERS", "8"))   # hilos de I/O de GCS (descarga/subida)
    workers = max(1, workers)
    if batch_size is None:
        batch_size = int(os.environ.get("PRIVACY_BATCH", "16"))  # imagenes por llamada de inferencia CPU
    batch_size = max(1, batch_size)
    checkpoint = max(1, int(os.environ.get("PRIVACY_CHECKPOINT", "500")))

    raw_bkt = settings.bkt_raw_user
    clean_bkt = settings.bkt_user_clean

    raw_images = [u for u in gcs.list(f"gs://{raw_bkt}/")
                  if "/images/" in u and u.lower().endswith(_IMG_EXTS)]
    done = _recently_processed(bq, settings)
    targets = [u for u in raw_images if _image_id(u, raw_bkt) not in done]
    if limit is not None:
        targets = targets[:limit]
    log.info("To anonymize: %d image(s) (raw=%s; %d already done in last 48h)",
             len(targets), raw_bkt, len(done))

    epoch = int(time.time())
    processed_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
    model_version = anonymizer.model_version

    audit_pending: List[Dict] = []
    catalog_pending: List[Dict] = []
    processed = faces = plates = failed = 0
    total = len(targets)
    meta_cache: Dict = {}

    def flush() -> None:
        """Vuelca lo pendiente a BigQuery (MERGE idempotente). No escribe en dry-run."""
        if not dry_run and (audit_pending or catalog_pending):
            if audit_pending:
                bq.upsert(settings.tbl_user_images_privacy, audit_pending, ["image_id"])
            if catalog_pending:
                bq.upsert(settings.tbl_images, catalog_pending, ["image_id"])
            log.info("Checkpoint: saved +%d audit / +%d catalog rows (%d/%d processed)",
                     len(audit_pending), len(catalog_pending), processed, total)
        audit_pending.clear()
        catalog_pending.clear()

    def _read(uri):
        try:
            return gcs.read_bytes(uri)
        except Exception:  # noqa: BLE001 - 404/transitorio -> se marca fallida
            return None

    def _write_clean(item):
        """(i, raw_uri, res) -> (i, clean_uri|None). Sube imagen limpia + copia lines."""
        i, raw_uri, res = item
        clean_uri = _clean_uri(raw_uri, raw_bkt, clean_bkt)
        try:
            if not dry_run:
                gcs.upload_bytes(clean_uri, res["clean"], "image/jpeg")
                raw_lines, clean_lines = _lines_uris(raw_uri, raw_bkt, clean_bkt)
                if gcs.exists(raw_lines):
                    gcs.copy(raw_lines, clean_lines)
            return i, clean_uri
        except Exception:  # noqa: BLE001
            return i, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in _chunks(targets, batch_size):
            raws = list(ex.map(_read, chunk))                      # bytes|None, alineado con chunk
            dec_idx = [i for i, b in enumerate(raws) if b is not None]
            res_by_i: Dict[int, Optional[Dict]] = {}
            if dec_idx:
                try:
                    batch_res = anonymizer.anonymize_batch([raws[i] for i in dec_idx])
                except Exception as e:  # noqa: BLE001 - fallo del lote -> todas fallidas, sigue
                    log.warning("batch inference failed: %s", e)
                    batch_res = [None] * len(dec_idx)
                for i, r in zip(dec_idx, batch_res):
                    res_by_i[i] = r
            # subir en paralelo las que tienen resultado
            up_items = [(i, chunk[i], res_by_i[i]) for i in dec_idx if res_by_i.get(i)]
            clean_by_i: Dict[int, Optional[str]] = dict(ex.map(_write_clean, up_items))
            # contabilizar en orden (solo el hilo principal toca los buffers)
            for i, raw_uri in enumerate(chunk):
                processed += 1
                res = res_by_i.get(i)
                clean_uri = clean_by_i.get(i)
                if res is None or clean_uri is None:
                    failed += 1
                    if failed <= 20:
                        log.warning("failed %s", raw_uri)
                else:
                    image_id = _image_id(raw_uri, raw_bkt)
                    user_id, session_id, frame_id = _ids(raw_uri[len(f"gs://{raw_bkt}/"):])
                    captured = _captured_at(gcs, raw_bkt, user_id, session_id, meta_cache)
                    faces += res["faces"]
                    plates += res["plates"]
                    audit_pending.append(
                        _audit_row(image_id, raw_uri, clean_uri, res["faces"], res["plates"],
                                   processed_at, model_version))
                    catalog_pending.append(
                        _catalog_row(image_id, clean_uri, res, user_id, session_id, frame_id,
                                     captured, processed_at))
                    if processed == 1:
                        print(f"[privacy] first image OK: {image_id} -> "
                              f"faces={res['faces']} plates={res['plates']} "
                              f"({res['width']}x{res['height']})", flush=True)
                if processed % 100 == 0:
                    log.info("Progress: %d/%d done (%d faces, %d plates, %d failed)",
                             processed, total, faces, plates, failed)
                if len(audit_pending) >= checkpoint:
                    flush()

    flush()  # vuelca lo que quede

    return {
        "candidates": total,
        "processed": processed,
        "anonymized": processed - failed,
        "faces_blurred": faces,
        "plates_blurred": plates,
        "failed": failed,
        "dry_run": dry_run,
        "workers": workers,
        "batch_size": batch_size,
        "model_version": model_version,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Anonymize user dashcam frames (faces/plates) with a local YOLOv8 detector on CPU.",
    )
    parser.add_argument("--dry-run", action="store_true", help="anonymize but do not write to GCS/BigQuery")
    parser.add_argument("--limit", type=int, default=None, help="process at most N images (smoke test)")
    parser.add_argument("--workers", type=int, default=None, help="parallel GCS I/O threads (default: PRIVACY_WORKERS env or 8)")
    parser.add_argument("--batch-size", type=int, default=None, help="images per CPU inference batch (default: PRIVACY_BATCH env or 16)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("ultralytics").setLevel(logging.WARNING)  # silencia el resumen por prediccion
    try:
        summary = run(dry_run=args.dry_run, limit=args.limit, workers=args.workers, batch_size=args.batch_size)
    except Exception:  # noqa: BLE001 - el job debe salir !=0 ante cualquier fallo
        log.exception("Anonymization failed")
        return 1
    log.info("OK %s", summary)
    return 1 if summary.get("failed") else 0  # imagenes fallidas -> salida !=0 (re-ejecutar resume)


if __name__ == "__main__":
    sys.exit(main())
