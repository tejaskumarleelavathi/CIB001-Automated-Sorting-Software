#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
select_closest_to_center.py — Pick the detection nearest image center
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

YOLO returns every box it finds in a frame; the robot only ever has one
specimen in the pipette. This is the (deliberately tiny) piece of logic
that bridges the two: given all detections in an EoA frame, pick the
one whose box center is closest to the image's own center pixel, and
treat its class as the frame's classification result -- the same
"rank center-first, take the first real answer" idea
cib/vision/eoa.py's classical pipeline already uses, just applied to
YOLO's boxes instead of contour blobs.

Not wired into the live orchestrator yet -- this is the reusable
building block for that integration, exercised standalone here (and by
inference_demo.py) against a trained model before touching
cib/vision/eoa.py at all.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Detection:
    class_name: str
    confidence: float
    x_center: float   # pixels, in the frame's own coordinate space
    y_center: float
    width: float
    height: float


def select_closest_to_center(
    detections: list[Detection], frame_width: int, frame_height: int,
) -> Detection | None:
    """
    Return the detection whose box center is nearest the frame's center
    pixel, or None if `detections` is empty (nothing detected at all --
    caller should treat this the same as today's NO_SPECIMEN case).
    """
    if not detections:
        return None
    fx, fy = frame_width / 2.0, frame_height / 2.0
    return min(
        detections,
        key=lambda d: (d.x_center - fx) ** 2 + (d.y_center - fy) ** 2,
    )


if __name__ == "__main__":
    # Quick sanity check with synthetic detections -- no model/image needed.
    dets = [
        Detection("DEBRIS", 0.81, x_center=50, y_center=50, width=20, height=20),
        Detection("BUG", 0.93, x_center=320, y_center=240, width=80, height=60),
        Detection("HAIR", 0.60, x_center=600, y_center=440, width=10, height=90),
    ]
    picked = select_closest_to_center(dets, frame_width=640, frame_height=480)
    print(f"Picked: {picked}")
    assert picked is not None and picked.class_name == "BUG"
    print("OK -- closest-to-center selection picked the centered BUG over the "
          "corner DEBRIS/HAIR, as expected.")
