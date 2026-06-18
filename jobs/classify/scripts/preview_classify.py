#!/usr/bin/env python
"""Clasifica N imagenes ALEATORIAS de un dataset (READ-ONLY) y muestra el resultado.

Indicas solo el dataset (la carpeta de primer nivel); funciona igual para cualquier
dataset y para usuarios, porque el layout normalizado es el mismo. Elige el bucket
con --source: public (bkt-prod-public-usc1) o user (bkt-prod-user-usc1).

Por cada imagen: la lee de GCS, la clasifica con Gemini (el SceneClassifier del job,
sobre el cliente general libs.vertex) e imprime {weather, scene, timeofday,
road_geometry}. Guarda en local la imagen + su .json para revisarla a ojo. No
escribe en el cloud ni en BigQuery.

Uso:
  python jobs/classify/scripts/preview_classify.py --dataset culane -n 5
  python jobs/classify/scripts/preview_classify.py --dataset culane --subdir test/curve/images -n 5
  python jobs/classify/scripts/preview_classify.py --dataset culane --subdir test/curve/images --quality high -n 5
  python jobs/classify/scripts/preview_classify.py --dataset <user_id> --source user -n 3

Nota: sin --subdir lista TODO el dataset para muestrear (lento en datasets grandes
como CULane). Acota con --subdir <split>/<categoria>/images para que sea rapido.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

# --- make `src` (this job) and `libs` (repo root) importable ---
_JOB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_JOB_ROOT, "..", ".."))
for _p in (_JOB_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Usa el trust store del SO -> TLS a GCP/Vertex tras un proxy corporativo.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

from libs.config import load_settings  # noqa: E402 - tras ajustar sys.path
from libs.gcs import GcsClient  # noqa: E402
from src.classifier import SceneClassifier  # noqa: E402

_IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify N random images of a dataset (read-only).")
    ap.add_argument("--dataset", required=True, help="dataset / top-level folder (e.g. culane, or a user id)")
    ap.add_argument("--source", default="public", choices=["public", "user"], help="bucket: public | user (default: public)")
    ap.add_argument("--subdir", default="", help="restrict listing to a subfolder under the dataset, e.g. test/curve/images (faster)")
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high"],
                    help="image resolution sent to Gemini (default: low = cheaper; higher = more detail/$)")
    ap.add_argument("-n", "--count", type=int, default=5, help="number of random images (default: 5)")
    ap.add_argument("--out", default="previews", help="local output dir (default: previews)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (reproducible sample)")
    args = ap.parse_args()

    settings = load_settings()
    gcs = GcsClient()
    bucket = settings.bkt_public if args.source == "public" else settings.bkt_user_clean
    # --subdir acota el listado (clave: sin el, lista TODO el dataset = lento).
    scope = f"{args.dataset}/{args.subdir.strip('/')}".rstrip("/")
    prefix = f"gs://{bucket}/{scope}/"

    uris = [u for u in gcs.list(prefix) if u.lower().endswith(_IMAGE_EXTS)]
    if not uris:
        raise SystemExit(f"No hay imagenes en {prefix} (¿bucket poblado? ¿dataset correcto?)")
    sample = random.Random(args.seed).sample(uris, min(args.count, len(uris)))

    os.makedirs(args.out, exist_ok=True)
    classifier = SceneClassifier.from_settings(settings, media_resolution=args.quality)
    print(f"Classifying {len(sample)} random image(s) from {prefix} ({len(uris)} found, quality={args.quality}) -> {args.out}/  [READ-ONLY]")

    for i, uri in enumerate(sample):
        image = gcs.read_bytes(uri)
        labels = classifier.classify(image)
        rel = uri.split(f"/{args.dataset}/", 1)[-1]
        print(f"[{i}] {rel}\n     {labels}")
        base = os.path.join(args.out, f"{args.dataset}_{i:02d}_" + rel.replace("/", "_"))
        with open(base, "wb") as fh:
            fh.write(image)
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(labels, fh)

    print(f"Done. {len(sample)} image(s) + .json in {args.out}/")


if __name__ == "__main__":
    main()
