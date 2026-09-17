#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crop_utils.py — Shared padded-crop logic for the YOLO EoA project
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

One crop-sizing rule used in two places: crop_classify_demo.py (crops
around a classically-localized blob at inference time) and
build_cls_dataset.py (crops around an annotated box at training-data
generation time). Both need to produce the SAME kind of crop -- same
padding relative to object size, same minimum size -- or a classifier
trained on one crop distribution and fed a differently-cropped one at
inference would be testing nothing. Centralizing it here is the
single-source-of-truth fix; two independent copies is exactly the trap
eoa_classifier.py's own docstring already warns about for its unrelated
duplicated constants.
"""
from __future__ import annotations

PAD_MULTIPLIER = 3.0   # crop side = max(bbox dimension) * this ...
MIN_CROP_PX = 600       # ... but never smaller than this, so tiny objects still get context


def crop_around_bbox(
    frame,
    cx: float, cy: float, bw: float, bh: float,
    pad_multiplier: float = PAD_MULTIPLIER,
    min_crop_px: int = MIN_CROP_PX,
):
    """Padded, roughly-square crop of `frame` centered on a pixel-space bbox
    (center cx,cy and size bw,bh, all in pixels)."""
    h, w = frame.shape[:2]
    side = max(max(bw, bh) * pad_multiplier, min_crop_px)
    x0 = int(max(0, cx - side / 2))
    y0 = int(max(0, cy - side / 2))
    x1 = int(min(w, cx + side / 2))
    y1 = int(min(h, cy + side / 2))
    return frame[y0:y1, x0:x1]
