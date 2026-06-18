#!/usr/bin/env python
"""Anonimiza N imagenes ALEATORIAS y guarda original + difuminada (SOLO PRUEBA).

Corre el detector YOLOv8 (CPU, por lotes) del job privacy sobre imagenes reales y
deja en local, por cada una, la original y la version con caras/matriculas
difuminadas, mas el conteo. Sirve para ajustar a ojo (conf, blur) antes de producir.

Fuentes (--source):
  - public: bucket de publicos normalizados (bkt-prod-public-usc1); indica --dataset
    (p. ej. culane) y opcional --subdir (p. ej. test/curve/images) para acotar/acelerar.
  - user: bucket CRUDO de usuario (bkt-prod-raw-user-usc1, la entrada real de privacy);
    --dataset = <user_id> (usr_...), opcional --subdir = <session_id>/images.

El modelo .pt no esta en local: si falta, se descarga del bucket de modelos
(gs://bkt-prod-models-usc1/...) usando tus credenciales (ADC). Necesitas las deps del
job instaladas (ultralytics, torch-cpu, opencv): pip install -r jobs/privacy/requirements.txt

Uso:
  python jobs/privacy/scripts/preview_privacy.py --source public --dataset culane --subdir test/curve/images -n 5
  python jobs/privacy/scripts/preview_privacy.py --source user --dataset usr_a1b2c3 -n 3
"""
from __future__ import annotations

import argparse
import os
import sys

# --- make `src` (this job) and `libs` (repo root) importable ---
_JOB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_JOB_ROOT, "..", ".."))
for _p in (_JOB_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Usa el trust store del SO -> TLS a GCP tras un proxy corporativo.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import random  # noqa: E402 - tras ajustar sys.path

from libs.config import load_settings  # noqa: E402
from libs.gcs import GcsClient  # noqa: E402
from src.anonymizer import Anonymizer  # noqa: E402

_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
_MODEL_URI = "gs://bkt-prod-models-usc1/privacy/dashcam-anon-yolov8-v1.pt"
_MODEL_PATH = os.path.join(_REPO_ROOT, "models", "dashcam-anon-yolov8-v1.pt")


def _ensure_model(gcs: GcsClient, model_path: str, model_uri: str) -> str:
    """Descarga el .pt del bucket de modelos si no esta en local (cachea)."""
    if os.path.exists(model_path):
        return model_path
    print(f"Model not found at {model_path}; downloading from {model_uri} ...")
    data = gcs.read_bytes(model_uri)
    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
    with open(model_path, "wb") as fh:
        fh.write(data)
    print(f"Saved model to {model_path} ({len(data) / 1e6:.1f} MB)")
    return model_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Anonymize N random images and save original + blurred (test only).")
    ap.add_argument("--source", default="public", choices=["public", "user"],
                    help="bucket: public (bkt-prod-public-usc1) | user (bkt-prod-raw-user-usc1). Default: public")
    ap.add_argument("--dataset", required=True, help="top-level folder: dataset (public, e.g. culane) or user_id (user)")
    ap.add_argument("--subdir", default="", help="restrict listing to a subfolder (faster), e.g. test/curve/images")
    ap.add_argument("-n", "--count", type=int, default=5, help="number of random images (default: 5)")
    ap.add_argument("--out", default="previews", help="local output dir (default: previews)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (reproducible sample)")
    ap.add_argument("--conf", type=float, default=0.1, help="detection confidence threshold (default: 0.1)")
    ap.add_argument("--model", default=_MODEL_PATH, help="local path to the YOLOv8 .pt (downloaded if missing)")
    args = ap.parse_args()

    settings = load_settings()
    gcs = GcsClient()
    bucket = settings.bkt_public if args.source == "public" else settings.bkt_raw_user
    scope = f"{args.dataset}/{args.subdir.strip('/')}".rstrip("/")
    prefix = f"gs://{bucket}/{scope}/"

    uris = [u for u in gcs.list(prefix) if u.lower().endswith(_IMAGE_EXTS)]
    if not uris:
        raise SystemExit(f"No images under {prefix} (¿bucket poblado? ¿source/dataset correctos?)")
    sample = random.Random(args.seed).sample(uris, min(args.count, len(uris)))

    model_path = _ensure_model(gcs, args.model, _MODEL_URI)
    anonymizer = Anonymizer.from_path(model_path, conf=args.conf)
    print(f"Model classes: {getattr(anonymizer, '_names', {})}")
    os.makedirs(args.out, exist_ok=True)
    print(f"Anonymizing {len(sample)} random image(s) from {prefix} ({len(uris)} found, conf={args.conf}) -> {args.out}/")

    raw_bytes = [gcs.read_bytes(u) for u in sample]
    results = anonymizer.anonymize_batch(raw_bytes)   # un solo lote (CPU)

    for i, (uri, raw, res) in enumerate(zip(sample, raw_bytes, results)):
        rel = uri.split(f"/{args.dataset}/", 1)[-1].replace("/", "_")
        base = os.path.join(args.out, f"{args.source}_{args.dataset}_{i:02d}_{rel}")
        if res is None:
            print(f"[{i}] {uri}\n     FAILED (decode/encode)")
            continue
        print(f"[{i}] {uri}\n     faces={res['faces']} plates={res['plates']} ({res['width']}x{res['height']})")
        with open(os.path.splitext(base)[0] + "_orig.jpg", "wb") as fh:
            fh.write(raw)
        with open(os.path.splitext(base)[0] + "_blur.jpg", "wb") as fh:
            fh.write(res["clean"])

    print(f"Done. original + blurred pairs in {args.out}/")


if __name__ == "__main__":
    main()
