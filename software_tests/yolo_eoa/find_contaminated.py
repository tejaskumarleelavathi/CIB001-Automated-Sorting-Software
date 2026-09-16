#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_contaminated.py — Detect EoA images with the old debug overlay baked in
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Some images in cib/output/eoa_images/backlight_on/ predate a 2026-09-07
fix that stopped drawing a debug crosshair/dot/offset-line directly onto
the saved "raw" frame (see cib/core/orchestrator.py's
_save_eoa_frames() docstring). The cutover happened mid-session that
day, so filename timestamps alone can't separate clean from
contaminated -- this instead detects the overlay itself: it's always
drawn dead center (w//2, h//2) in a near-pure green ((B,G,R) ~ (1,255,1)),
a color that never occurs naturally in these backlit specimen photos.

Usage:
    python find_contaminated.py [image_dir]

Prints one contaminated filename per line to stdout (nothing else), so
it composes with other tools, e.g.:
    python find_contaminated.py dataset/images > contaminated.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

DEFAULT_DIR = Path(__file__).parent / "dataset" / "images"
CENTER_WINDOW = 150   # pixels each side of image center to check
GREEN_PIXEL_THRESHOLD = 30   # near-pure-green pixel count above which we call it contaminated


def is_contaminated(image_path: Path) -> bool:
    img = cv2.imread(str(image_path))
    if img is None:
        return False
    h, w = img.shape[:2]
    cy, cx = h // 2, w // 2
    y0, y1 = max(0, cy - CENTER_WINDOW), min(h, cy + CENTER_WINDOW)
    x0, x1 = max(0, cx - CENTER_WINDOW), min(w, cx + CENTER_WINDOW)
    region = img[y0:y1, x0:x1]
    b, g, r = region[:, :, 0].astype(int), region[:, :, 1].astype(int), region[:, :, 2].astype(int)
    mask = (g > 220) & (b < 40) & (r < 40)
    return int(mask.sum()) > GREEN_PIXEL_THRESHOLD


def main() -> None:
    image_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.png"))
    contaminated = [f for f in files if is_contaminated(f)]
    for f in contaminated:
        print(f.name)
    print(f"\n{len(contaminated)} / {len(files)} images contaminated", file=sys.stderr)


if __name__ == "__main__":
    main()
