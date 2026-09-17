#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_yolo_cls.py — Train a YOLOv8 whole-crop classifier on per-box EoA crops
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Run after build_cls_dataset.py has produced dataset_cls/{train,val,test}/
<CLASS>/*.jpg. This is a classification model (yolov8n-cls), not the
detector train_yolo.py trains -- it outputs one label + confidence for
the whole input image, no boxes. That's the point: crop_classify_demo.py
already localizes the one specimen that matters before this ever runs,
so there's nothing left for a detector's boxes/NMS to do.

yolov8n-cls (nano) is the default for the same reason train_yolo.py
defaults to yolov8n: inference has to fit inside the cycle-time budget,
and nano is the cheapest variant that still does real classification.

Install first (not part of the HMI's normal deploy -- training only):
    pip install -e ".[ml]"

Usage:
    python software_tests/yolo_eoa/train_yolo_cls.py [--epochs 50] [--model yolov8n-cls.pt]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import DATASET_DIR, RUNS_DIR  # noqa: E402

CLS_DATASET_DIR = DATASET_DIR.parent / "dataset_cls"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=224)
    parser.add_argument("--model", default="yolov8n-cls.pt",
                         help="Starting weights -- yolov8n-cls.pt (nano, default) / "
                              "yolov8s-cls.pt (small, if nano isn't accurate enough)")
    parser.add_argument("--batch", type=int, default=32)
    args = parser.parse_args()

    if not CLS_DATASET_DIR.exists():
        raise SystemExit(f"{CLS_DATASET_DIR} not found -- run build_cls_dataset.py first.")

    from ultralytics import YOLO  # imported here so --help works without ultralytics installed

    model = YOLO(args.model)
    model.train(
        data=str(CLS_DATASET_DIR),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        project=str(RUNS_DIR),
        name="eoa_cls",
    )


if __name__ == "__main__":
    main()
