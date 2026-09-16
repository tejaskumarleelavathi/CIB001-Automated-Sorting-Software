#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_yolo.py — Train a YOLOv8 detector on annotated EoA images
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Run after split_dataset.py has produced dataset/data.yaml. Not runnable
until real annotations exist -- this is deliberately a thin wrapper
around ultralytics' own training loop, not a rewrite of it.

yolov8n (nano) is the default: it's the model this project should ship
with unless real hardware testing shows it isn't accurate enough --
inference eventually has to run somewhere in the cycle loop without
blowing out the cycle-time budget this whole codebase has been
carefully tuned around all session, and nano is the cheapest variant
that still does real object detection. Only move up to yolov8s/m if
accuracy on the val set doesn't clear the classical pipeline's bar.

Install first (not part of the HMI's normal deploy -- training only):
    pip install -e ".[ml]"

Usage:
    python software_tests/yolo_eoa/train_yolo.py [--epochs 100] [--model yolov8n.pt]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import DATASET_DIR, RUNS_DIR  # noqa: E402

HERE = Path(__file__).parent
DATA_YAML = DATASET_DIR / "data.yaml"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--model", default="yolov8n.pt",
                         help="Starting weights -- yolov8n.pt (nano, default) / "
                              "yolov8s.pt (small, if nano isn't accurate enough)")
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    if not DATA_YAML.exists():
        raise SystemExit(
            f"{DATA_YAML} not found -- run prepare_dataset.py, annotate with LabelImg, "
            "then split_dataset.py first."
        )

    from ultralytics import YOLO  # imported here so --help works without ultralytics installed

    model = YOLO(args.model)
    model.train(
        data=str(DATA_YAML),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        project=str(RUNS_DIR),
        name="eoa_yolo",
    )


if __name__ == "__main__":
    main()
