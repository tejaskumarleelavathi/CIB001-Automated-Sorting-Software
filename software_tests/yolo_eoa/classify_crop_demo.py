#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify_crop_demo.py — Whole-crop classification, no detection/NMS needed
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Same localization as crop_classify_demo.py (classical center-most blob,
then a padded crop via crop_utils.py), but classifies the WHOLE crop
directly with a classification model (train_yolo_cls.py) instead of
running a detector and picking a box out of its results. There's
nothing to tie-break here: one crop in, one label + confidence out --
no boxes, no NMS, no closest-to-center logic needed at all.

Controls (focus must be on the image window):
    any key / SPACE / N  -> next image
    P                     -> previous image
    Q / ESC               -> quit

Usage:
    python software_tests/yolo_eoa/classify_crop_demo.py
    python software_tests/yolo_eoa/classify_crop_demo.py --pad 4.0 --min-crop 800
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.resolve().parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT))

from _paths import RUNS_DIR  # noqa: E402
from crop_utils import crop_around_bbox, PAD_MULTIPLIER, MIN_CROP_PX  # noqa: E402
from crop_classify_demo import _show  # noqa: E402 -- reuse the same viewer/keys, don't duplicate it
from cib.vision.eoa import (  # noqa: E402
    _to_gray, _segment, _morph_close, _find_blobs,
    _rank_candidates, _intensity_features, _classify,
)

DEFAULT_SOURCE = _REPO_ROOT / "cib" / "output" / "eoa_images" / "backlight_on"
DEFAULT_WEIGHTS = RUNS_DIR / "eoa_cls" / "weights" / "best.pt"
WINDOW_NAME = "Crop classification vs classical"
# Display scaling is handled by the imported _show() -- see its own
# MAX_DISPLAY_DIM in crop_classify_demo.py.


def crop_around_blob(frame, blob: dict, pad_multiplier: float, min_crop_px: int):
    """Same crop as crop_classify_demo.py -- must match, see crop_utils.py."""
    import cv2
    x, y, bw, bh = cv2.boundingRect(blob["contour"])
    cx, cy = x + bw / 2.0, y + bh / 2.0
    return crop_around_bbox(frame, cx, cy, bw, bh, pad_multiplier, min_crop_px)


def draw_result(cv2, crop, label: str, conf: float, top3: list):
    """Banner with the picked label/confidence plus the full 3-class
    probability breakdown -- there are no boxes to draw, so this is just text."""
    out = crop.copy()
    scale = max(0.9, out.shape[1] / 1200)
    banner_h = int(150 * scale)
    cv2.rectangle(out, (0, 0), (out.shape[1], banner_h), (20, 20, 20), -1)
    cv2.putText(out, f"{label}  {conf:.2f}", (10, int(45 * scale)),
                cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 220, 0), max(2, int(scale * 2)))
    y = int(80 * scale)
    for name, p in top3:
        cv2.putText(out, f"{name}: {p:.2f}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, scale * 0.6, (210, 210, 210), 1)
        y += int(28 * scale)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--pad", type=float, default=PAD_MULTIPLIER,
                         help="Crop side = blob bbox dimension * this")
    parser.add_argument("--min-crop", type=int, default=MIN_CROP_PX,
                         help="Crop side never smaller than this (px)")
    args = parser.parse_args()

    if not args.weights.exists():
        raise SystemExit(f"{args.weights} not found -- train a classifier first "
                          f"(train_yolo_cls.py).")
    if not args.source.exists():
        raise SystemExit(f"{args.source} not found.")

    import cv2
    import numpy as np
    from ultralytics import YOLO

    files = sorted(args.source.glob("*.jpg")) + sorted(args.source.glob("*.png"))
    if not files:
        raise SystemExit(f"No .jpg/.png images found in {args.source}")

    print(f"{len(files)} images, weights={args.weights.name}, "
          f"pad={args.pad}x, min_crop={args.min_crop}px")
    print("Controls: any key/SPACE/N = next, P = previous, Q/ESC = quit\n")

    model = YOLO(str(args.weights))
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    i = 0
    while 0 <= i < len(files):
        f = files[i]
        frame = cv2.imread(str(f))
        header = f"[{i + 1}/{len(files)}] {f.name}"

        gray = _to_gray(frame)
        binary, _ = _segment(gray)
        original_binary = binary.copy()
        closed = _morph_close(binary)
        blobs = _find_blobs(closed)

        if not blobs:
            print(f"{header} -- no blobs found (classical segmentation)")
            step = _show(cv2, np, WINDOW_NAME, None, header + " -- NO BLOBS")
            if step == 0:
                break
            i = max(0, i + step)
            continue

        ranked = _rank_candidates(blobs, gray.shape)
        blob = ranked[0]   # center-most -- the one the pipette is actually over
        _intensity_features(blob, gray, frame, original_binary)
        classical_label, classical_reason = _classify(blob)

        crop = crop_around_blob(frame, blob, args.pad, args.min_crop)
        result = model.predict(source=crop, verbose=False)[0]
        top1 = int(result.probs.top1)
        conf = float(result.probs.top1conf)
        label = model.names[top1]
        top3 = [
            (model.names[int(idx)], float(result.probs.data[idx]))
            for idx in result.probs.top5[:3]
        ]

        agree = "MATCH" if label == classical_label else "DIFFER"
        print(f"{header}")
        print(f"    classical: {classical_label:8s} ({classical_reason})")
        print(f"    yolo-cls:  {label:8s} conf={conf:.2f}  "
              f"top3={[(n, round(p, 2)) for n, p in top3]}")
        print(f"    -> {agree}")

        annotated = draw_result(cv2, crop, label, conf, top3)
        title = f"{header} | classical={classical_label} yolo-cls={label} [{agree}]"
        step = _show(cv2, np, WINDOW_NAME, annotated, title)
        if step == 0:
            break
        i = max(0, i + step)

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
