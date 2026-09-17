#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crop_classify_demo.py — Classify only the classical pipeline's center-most
blob, via a cropped-region YOLO call, instead of whole-frame YOLO detection
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Why crop first
--------------
Running YOLO on the full EoA frame returns every blob it can find (bugs,
hair, debris fragments scattered across the image) -- then you need a
second decision (closest-to-center, see select_closest_to_center.py) to
figure out which one is actually the specimen under the pipette. That
second decision is redundant: cib/vision/eoa.py's classical pipeline
already does exactly that localization step (segment -> find contour
blobs -> rank center-first), just using shape/intensity heuristics
(_classify()) for the label instead of a trained model.

This script reuses that SAME localization (imported directly from
cib/vision/eoa.py, not reimplemented -- avoids the "keep two copies of
the tuned constants in sync" trap eoa_classifier.py already warns
about), takes only the top-ranked (center-most) blob, crops a padded
region of the ORIGINAL frame around it, and asks YOLO to classify only
that crop. YOLO then only ever sees one candidate region per frame, so
there's no whole-image closest-to-center ambiguity to resolve at all.

Known open question (call this out, don't hide it): the model was
trained on FULL, uncropped 5320x4600 frames, where a specimen is a
small fraction of the frame. A tight crop presents that same specimen
as a much larger fraction of a 640x640 resized input -- a scale the
model never explicitly trained on. --pad/--min-crop below control how
much surrounding context is kept to soften that mismatch; tune them by
watching real results with this script, not by guessing.

Controls (focus must be on the image window):
    any key / SPACE / N  -> next image
    P                     -> previous image
    Q / ESC               -> quit

Usage:
    python software_tests/yolo_eoa/crop_classify_demo.py
    python software_tests/yolo_eoa/crop_classify_demo.py --pad 4.0 --min-crop 800
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
from select_closest_to_center import Detection, select_closest_to_center  # noqa: E402
from cib.vision.eoa import (  # noqa: E402
    _to_gray, _segment, _morph_close, _find_blobs,
    _rank_candidates, _intensity_features, _classify,
)

DEFAULT_SOURCE = _REPO_ROOT / "cib" / "output" / "eoa_images" / "backlight_on"
DEFAULT_WEIGHTS = RUNS_DIR / "eoa_yolo" / "weights" / "best.pt"
WINDOW_NAME = "Crop + YOLO vs classical"
MAX_DISPLAY_DIM = 900

PAD_MULTIPLIER = 3.0   # crop side = max(blob bbox dimension) * this ...
MIN_CROP_PX = 600      # ... but never smaller than this, so tiny debris still gets context


def crop_around_blob(frame, blob: dict, pad_multiplier: float, min_crop_px: int):
    """Padded, roughly-square crop of `frame` centered on `blob`'s bounding box."""
    import cv2
    h, w = frame.shape[:2]
    x, y, bw, bh = cv2.boundingRect(blob["contour"])
    cx, cy = x + bw / 2.0, y + bh / 2.0
    side = max(max(bw, bh) * pad_multiplier, min_crop_px)
    x0 = int(max(0, cx - side / 2))
    y0 = int(max(0, cy - side / 2))
    x1 = int(min(w, cx + side / 2))
    y1 = int(min(h, cy + side / 2))
    return frame[y0:y1, x0:x1]


def _show(cv2, np, window_name: str, image, title: str) -> int:
    """Display `image` (or a blank frame if None), wait for a key, return
    +1/-1/0 for next/previous/quit."""
    if image is None:
        image = np.zeros((300, 900, 3), dtype="uint8")

    h, w = image.shape[:2]
    scale = min(MAX_DISPLAY_DIM / w, MAX_DISPLAY_DIM / h, 1.0)
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)))

    cv2.setWindowTitle(window_name, title)
    cv2.imshow(window_name, image)
    key = cv2.waitKey(0) & 0xFF
    if key in (ord("q"), 27):
        return 0
    if key == ord("p"):
        return -1
    return 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--pad", type=float, default=PAD_MULTIPLIER,
                         help="Crop side = blob bbox dimension * this")
    parser.add_argument("--min-crop", type=int, default=MIN_CROP_PX,
                         help="Crop side never smaller than this (px)")
    args = parser.parse_args()

    if not args.weights.exists():
        raise SystemExit(f"{args.weights} not found -- train a model first (train_yolo.py).")
    if not args.source.exists():
        raise SystemExit(f"{args.source} not found.")

    import cv2
    import numpy as np
    from ultralytics import YOLO

    files = sorted(args.source.glob("*.jpg")) + sorted(args.source.glob("*.png"))
    if not files:
        raise SystemExit(f"No .jpg/.png images found in {args.source}")

    print(f"{len(files)} images, weights={args.weights.name}, conf>={args.conf}, "
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
        result = model.predict(source=crop, conf=args.conf, verbose=False)[0]

        dets = [
            Detection(
                class_name=model.names[int(b.cls)],
                confidence=float(b.conf),
                x_center=float(b.xywh[0][0]), y_center=float(b.xywh[0][1]),
                width=float(b.xywh[0][2]), height=float(b.xywh[0][3]),
            )
            for b in result.boxes
        ]
        ch, cw = crop.shape[:2]
        picked = select_closest_to_center(dets, cw, ch)
        yolo_label = picked.class_name if picked else "NO_DETECTION"
        yolo_conf = f"{picked.confidence:.2f}" if picked else "n/a"

        agree = "MATCH" if picked and picked.class_name == classical_label else "DIFFER"
        print(f"{header}")
        print(f"    classical: {classical_label:8s} ({classical_reason})")
        print(f"    yolo:      {yolo_label:8s} conf={yolo_conf}  [{len(dets)} det(s) in crop]")
        print(f"    -> {agree}")

        annotated = result.plot()
        title = f"{header} | classical={classical_label} yolo={yolo_label} [{agree}]"
        step = _show(cv2, np, WINDOW_NAME, annotated, title)
        if step == 0:
            break
        i = max(0, i + step)

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
