#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_cls_dataset.py — Turn box annotations into a whole-crop classification dataset
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Detection + NMS + closest-to-center (crop_classify_demo.py) is more
machinery than this pipeline needs: cib/vision/eoa.py's classical
localization already finds the one specimen that matters before YOLO
ever runs, so a detector's ability to find MULTIPLE objects in a frame
is wasted, and its NMS/duplicate-box problems (see crop_classify_demo.py
git history) are self-inflicted. A whole-image classifier just answers
"what is this crop" -- no boxes, no NMS, no tie-break.

This script builds that classifier's training data: for every annotated
box (dataset/labels/<split>/*.txt, YOLO format) it crops the ORIGINAL
image around that box using the exact same padding rule
crop_classify_demo.py uses at inference time (crop_utils.py -- kept in
one place on purpose, see that module's docstring), and writes it into
the folder-per-class layout ultralytics' classification mode expects:

    <DATASET_DIR's parent>/dataset_cls/train/<CLASS>/<name>_<box_idx>.jpg
    .../val/<CLASS>/...
    .../test/<CLASS>/...

An image with N annotated boxes produces N crops (one per box) -- this
is expected to noticeably grow the effective example count versus the
detection dataset, since dataset/labels/train alone has images with up
to 11 boxes.

Usage:
    python software_tests/yolo_eoa/build_cls_dataset.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import DATASET_DIR  # noqa: E402
from crop_utils import crop_around_bbox  # noqa: E402

# Index must match classes.txt / dataset/data.yaml's `names` order --
# that's what the class id in each label line refers to.
CLASSES = ["BUG", "HAIR", "DEBRIS"]
CLS_DATASET_DIR = DATASET_DIR.parent / "dataset_cls"


def main() -> None:
    import cv2

    for split in ("train", "val", "test"):
        img_dir = DATASET_DIR / "images" / split
        lbl_dir = DATASET_DIR / "labels" / split
        if not img_dir.exists() or not lbl_dir.exists():
            continue

        counts = {c: 0 for c in CLASSES}
        for label_path in sorted(lbl_dir.glob("*.txt")):
            image_path = img_dir / f"{label_path.stem}.jpg"
            if not image_path.exists():
                image_path = img_dir / f"{label_path.stem}.png"
            if not image_path.exists():
                print(f"  skip {label_path.name} -- no matching image")
                continue

            frame = cv2.imread(str(image_path))
            if frame is None:
                print(f"  skip {label_path.name} -- image failed to load")
                continue
            h, w = frame.shape[:2]

            lines = [
                ln.split() for ln in label_path.read_text().splitlines() if ln.strip()
            ]
            for idx, parts in enumerate(lines):
                cls_id = int(parts[0])
                xc, yc, bw, bh = (float(v) for v in parts[1:5])
                crop = crop_around_bbox(
                    frame, xc * w, yc * h, bw * w, bh * h,
                )
                class_name = CLASSES[cls_id]
                out_dir = CLS_DATASET_DIR / split / class_name
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{label_path.stem}_{idx}.jpg"
                cv2.imwrite(str(out_path), crop)
                counts[class_name] += 1

        total = sum(counts.values())
        print(f"{split}: {total} crops -- " + ", ".join(f"{c}={n}" for c, n in counts.items()))

    print(f"\nWritten to {CLS_DATASET_DIR}")


if __name__ == "__main__":
    main()
