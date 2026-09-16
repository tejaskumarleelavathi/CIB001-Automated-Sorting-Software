#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_dataset.py — Stage EoA images for YOLO bounding-box annotation
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Copies the backlight-ON EoA "keeper" frames (the classification target
frame this YOLO model is meant to replace/improve on — see
cib/vision/eoa.py's classical BUG/HAIR/DEBRIS classifier) into a flat
pool ready for annotation with LabelImg, alongside classes.txt so
LabelImg's class list and YOLO label indices line up automatically.

Source is read-only — this only copies, never moves or deletes, so the
original run archive under cib/output/eoa_images/ is untouched.

Usage:
    python software_tests/yolo_eoa/prepare_dataset.py

Then annotate:
    pip install -e ".[ml]"
    labelImg software_tests/yolo_eoa/dataset/images software_tests/yolo_eoa/classes.txt

LabelImg: set the save format to YOLO (View > Auto Save mode helps), and
save each image's labels into software_tests/yolo_eoa/dataset/images
alongside the image (same filename, .txt extension) — that's where
split_dataset.py expects to find them next.
"""
from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = REPO_ROOT / "cib" / "output" / "eoa_images" / "backlight_on"
DEST_DIR = Path(__file__).parent / "dataset" / "images"


def main() -> None:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"Source directory not found: {SOURCE_DIR}")

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    source_images = sorted(SOURCE_DIR.glob("*.jpg")) + sorted(SOURCE_DIR.glob("*.png"))
    if not source_images:
        raise SystemExit(f"No .jpg/.png images found in {SOURCE_DIR}")

    copied = 0
    skipped = 0
    for src in source_images:
        dest = DEST_DIR / src.name
        if dest.exists():
            skipped += 1
            continue
        shutil.copy2(src, dest)
        copied += 1

    print(f"Source:  {SOURCE_DIR}  ({len(source_images)} images found)")
    print(f"Dest:    {DEST_DIR}")
    print(f"Copied:  {copied}")
    print(f"Skipped: {skipped} (already present)")
    print()
    print("Next: annotate with LabelImg, then run split_dataset.py")


if __name__ == "__main__":
    main()
