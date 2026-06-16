"""Previsualiza el GT producido por la normalizacion de ingest-public (SOLO LECTURA).

Lee N muestras de un dataset directamente desde raw-public, construye el
.lines.json normalizado igual que haria el job y escribe una preview LOCAL por
muestra para comprobar que los carriles encajan con la imagen:
  - <out>/<name>.lines.json   GT normalizado (estilo CurveLanes, coords enteras)
  - <out>/<name>.png          imagen origen con los carriles dibujados (Pillow)

NUNCA escribe en cloud: solo lee GCS; toda salida es un archivo local.

Uso (desde la raiz del repo o desde cualquier ruta):
  python jobs/ingest-public/scripts/preview_gt.py --dataset culane -n 5
  python jobs/ingest-public/scripts/preview_gt.py -n 3 --split train --out previews
  # omite --dataset para elegir automaticamente el primer descriptor valido en _descriptors/
"""
from __future__ import annotations

import argparse
import io
import os
import sys

# hace importables `src` (este job) y `libs` (raiz del repo)
_JOB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_JOB_ROOT, "..", ".."))
for _p in (_JOB_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Usa el trust store del SO para que TLS contra GCP funcione detras de un proxy corporativo.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import yaml  # noqa: E402

from libs.config import load_settings  # noqa: E402
from libs.gcs import GcsClient  # noqa: E402
from src.adapters.base import format_label_json, to_label_json  # noqa: E402
from src.registry import get_adapter  # noqa: E402

try:
    from PIL import Image, ImageDraw  # noqa: E402
except Exception:
    Image = ImageDraw = None  # type: ignore[assignment]

# Un color por carril (RGB), rotando la paleta.
PALETTE = [(255, 0, 0), (0, 200, 0), (0, 128, 255), (255, 160, 0), (200, 0, 200), (0, 200, 200)]


def pick_dataset(gcs: GcsClient, settings, dataset: str | None) -> str:
    """Devuelve el dataset dado, o elige automaticamente el primer descriptor valido."""
    if dataset:
        return dataset
    prefix = settings.descriptors_prefix.strip("/") + "/"
    for uri in gcs.list(f"gs://{settings.bkt_raw_public}/{prefix}"):
        name = uri.rsplit("/", 1)[-1]
        if not name.endswith(".yml") or name == "default.yml":
            continue
        try:
            descriptor = yaml.safe_load(gcs.read_text(uri)) or {}
        except Exception:
            continue
        if descriptor.get("adapter"):
            return name[: -len(".yml")]
    raise SystemExit("No valid <dataset>.yml under _descriptors/. Pass --dataset.")


def draw_lanes(image_bytes: bytes, lines: list) -> "Image.Image":
    """Dibuja cada carril (polilinea + puntos) sobre la imagen origen."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for i, lane in enumerate(lines):
        color = PALETTE[i % len(PALETTE)]
        points = [(p["x"], p["y"]) for p in lane]
        if len(points) >= 2:
            draw.line(points, fill=color, width=4)
        for x, y in points:
            draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=color)
    return img


def report(index: int, sample, gt: dict, has_label: bool) -> None:
    """Imprime un resumen rapido de sanity del GT generado."""
    lines = gt["Lines"]
    leaf = f"{sample.split}/{sample.category}" if sample.category else sample.split
    bounds = ""
    xs = [p["x"] for lane in lines for p in lane]
    ys = [p["y"] for lane in lines for p in lane]
    if xs and sample.width and sample.height:
        in_bounds = all(0 <= x <= sample.width for x in xs) and all(0 <= y <= sample.height for y in ys)
        bounds = (
            f" | x[{min(xs)}..{max(xs)}] y[{min(ys)}..{max(ys)}] "
            f"dims {sample.width}x{sample.height} in_bounds={in_bounds}"
        )
    tag = "" if has_label else "  [no GT]"
    print(f"[{index}] {leaf}  {sample.rel_path}{tag}")
    print(f"     lanes={len(lines)} points/lane={[len(line) for line in lines]}{bounds}")


def compare_to_native(gcs: GcsClient, sample, lanes, gt: dict, n_points: int) -> None:
    """Muestra la etiqueta nativa junto al GT generado para poder revisar puntos.

    `lanes` son los puntos tal como se parsean del formato nativo (float); gt['Lines']
    los contiene redondeados a int. La cadena es: archivo nativo -> parseado (orig)
    -> GT (redondeado). Los carriles se corresponden uno a uno.
    """
    if sample.label_uri:
        try:
            raw = gcs.read_text(sample.label_uri)
            print(f"     native {sample.label_uri.rsplit('/', 1)[-1]}: {raw[:160].strip()}")
        except Exception as e:  # noqa: BLE001
            print(f"     (native label unavailable: {type(e).__name__})")
    for li, (orig_lane, gt_lane) in enumerate(zip(lanes, gt["Lines"])):
        pairs = "  ".join(
            f"({ox:g},{oy:g})->({p['x']},{p['y']})"
            for (ox, oy), p in zip(orig_lane[:n_points], gt_lane[:n_points])
        )
        print(f"       L{li} orig->GT: {pairs}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview generated GT lines (read-only; writes nothing to cloud).")
    ap.add_argument("--dataset", default=None, help="dataset name (default: first valid descriptor)")
    ap.add_argument("-n", "--count", type=int, default=5, help="number of examples (default: 5)")
    ap.add_argument("--split", default=None, help="only this split: train | val | test")
    ap.add_argument("--out", default="previews", help="local output dir (default: previews)")
    ap.add_argument("--points", type=int, default=4, help="points/lane shown in the native->GT compare (default: 4)")
    ap.add_argument("--include-unlabeled", action="store_true",
                    help="also show images without GT (e.g. CurveLanes test); lists images/ even without a list file")
    args = ap.parse_args()

    settings = load_settings()
    gcs = GcsClient()
    dataset = pick_dataset(gcs, settings, args.dataset)

    descriptor = yaml.safe_load(gcs.read_text(settings.descriptor_uri(dataset))) or {}
    adapter_name = descriptor.get("adapter")
    if not adapter_name:
        raise SystemExit(f"descriptor for {dataset!r} has no 'adapter'")
    if args.include_unlabeled:
        descriptor.setdefault("options", {})["ingest_unlabeled"] = True
    adapter = get_adapter(adapter_name)(descriptor, gcs, settings.raw_public_uri(dataset))

    os.makedirs(args.out, exist_ok=True)
    print(
        f"Dataset {dataset!r} (adapter {adapter_name}); up to {args.count} example(s) -> {args.out}/  "
        f"[READ-ONLY, nothing written to the cloud]"
    )
    if Image is None:
        print("  Pillow not installed -> JSON only (no overlay). Run: pip install pillow")

    epoch = 1700000000  # fixed so previews are deterministic
    want_splits = {args.split} if args.split else None
    written = 0
    for sample in adapter.iter_samples(want_splits):
        if written >= args.count:
            break
        lanes = adapter.read_label(sample.label_uri)
        has_label = lanes is not None
        if not has_label and not args.include_unlabeled:
            continue

        gt = to_label_json(lanes or [], epoch)
        report(written, sample, gt, has_label)
        base = f"{dataset}_{written:02d}_{sample.frame_id}"

        with open(os.path.join(args.out, base + ".lines.json"), "w", encoding="utf-8") as fh:
            fh.write(format_label_json(gt))
        if has_label:
            compare_to_native(gcs, sample, lanes, gt, args.points)

        if Image is not None:
            try:
                img = draw_lanes(gcs.read_bytes(sample.src_image_uri), gt["Lines"])
                img.save(os.path.join(args.out, base + ".png"))
            except Exception as e:  # noqa: BLE001
                print(f"     (could not render image: {type(e).__name__}: {e})")
        written += 1

    if written == 0:
        print("No labeled samples matched the filters.")
    else:
        print(f"Done. {written} preview(s) written to {args.out}/")


if __name__ == "__main__":
    main()
