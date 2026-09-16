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
pool ready for annotation, alongside classes.txt.

Skips images with the old debug overlay baked in (crosshair/dot/offset
line drawn dead-center before a 2026-09-07 fix, mid-session that day --
see find_contaminated.py) -- confirmed 889 of the first 1,190 raw-named
files still have it, so this check is not optional. That overlay's
pure-green pixels would otherwise train the model to key off a green
crosshair that will never appear in a real inference frame.

Source is read-only — this only copies, never moves or deletes, so the
original run archive under cib/output/eoa_images/ is untouched.

Usage:
    python software_tests/yolo_eoa/prepare_dataset.py

Then annotate:
    <path to cib_ml env>/python.exe software_tests/yolo_eoa/annotate.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from find_contaminated import is_contaminated  # noqa: E402

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
    skipped_existing = 0
    skipped_contaminated = 0
    for src in source_images:
        dest = DEST_DIR / src.name
        if dest.exists():
            skipped_existing += 1
            continue
        if is_contaminated(src):
            skipped_contaminated += 1
            continue
        shutil.copy2(src, dest)
        copied += 1

    print(f"Source:              {SOURCE_DIR}  ({len(source_images)} images found)")
    print(f"Dest:                {DEST_DIR}")
    print(f"Copied:              {copied}")
    print(f"Skipped (existing):  {skipped_existing}")
    print(f"Skipped (overlay):   {skipped_contaminated}")
    print()
    print("Next: annotate with annotate.py, then run split_dataset.py")


if __name__ == "__main__":
    main()
