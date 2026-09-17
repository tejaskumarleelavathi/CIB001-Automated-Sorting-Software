#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inference_demo.py — Step through detections one image at a time
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Manual visual spot-check tool: runs the trained model over a folder of
EoA images and opens a window per image showing the drawn boxes,
labels, and confidences, so a person can eyeball what the model
actually saw instead of only trusting the aggregate mAP/precision/
recall numbers from model.val(). This is the demo harness
select_closest_to_center.py's docstring refers to.

Controls (focus must be on the image window):
    any key / SPACE / N  -> next image
    P                     -> previous image
    Q / ESC               -> quit

Usage:
    python software_tests/yolo_eoa/inference_demo.py
    python software_tests/yolo_eoa/inference_demo.py --source path\\to\\images --conf 0.3
    python software_tests/yolo_eoa/inference_demo.py --weights path\\to\\best.pt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import RUNS_DIR  # noqa: E402

# Default source: every EoA-with-backlight frame captured so far (includes
# both clean images and the pre-2026-09-07 overlay-marked ones -- see
# find_contaminated.py) -- the same pool discussed live and spot-checked
# manually before writing this script.
DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "cib" / "output" / "eoa_images" / "backlight_on"
DEFAULT_WEIGHTS = RUNS_DIR / "eoa_yolo" / "weights" / "best.pt"
WINDOW_NAME = "YOLO EoA detections"
MAX_DISPLAY_DIM = 1200   # source frames are 5320x4600 -- scale down to fit a screen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                         help=f"Directory of images to step through (default: {DEFAULT_SOURCE})")
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS,
                         help=f"Model weights (default: {DEFAULT_WEIGHTS})")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    args = parser.parse_args()

    if not args.weights.exists():
        raise SystemExit(f"{args.weights} not found -- train a model first (train_yolo.py).")
    if not args.source.exists():
        raise SystemExit(f"{args.source} not found.")

    import cv2
    from ultralytics import YOLO

    files = sorted(args.source.glob("*.jpg")) + sorted(args.source.glob("*.png"))
    if not files:
        raise SystemExit(f"No .jpg/.png images found in {args.source}")

    print(f"{len(files)} images, weights={args.weights.name}, conf>={args.conf}")
    print("Controls: any key/SPACE/N = next, P = previous, Q/ESC = quit\n")

    model = YOLO(str(args.weights))
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    i = 0
    while 0 <= i < len(files):
        f = files[i]
        result = model.predict(source=str(f), conf=args.conf, verbose=False)[0]
        annotated = result.plot()  # BGR numpy array with boxes/labels/conf already drawn

        h, w = annotated.shape[:2]
        scale = min(MAX_DISPLAY_DIM / w, MAX_DISPLAY_DIM / h, 1.0)
        if scale < 1.0:
            annotated = cv2.resize(annotated, (int(w * scale), int(h * scale)))

        n = len(result.boxes)
        title = f"[{i + 1}/{len(files)}] {f.name} -- {n} detection{'s' if n != 1 else ''}"
        cv2.setWindowTitle(WINDOW_NAME, title)
        cv2.imshow(WINDOW_NAME, annotated)

        print(title)
        for b in result.boxes:
            print(f"    {model.names[int(b.cls)]:8s} conf={float(b.conf):.2f}")

        key = cv2.waitKey(0) & 0xFF
        if key in (ord("q"), 27):       # Q or ESC
            break
        elif key == ord("p"):
            i = max(0, i - 1)
        else:                            # any other key (incl. space/N) -> next
            i += 1

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
