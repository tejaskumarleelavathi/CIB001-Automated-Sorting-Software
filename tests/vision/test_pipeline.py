"""Tests for the overhead vision pipeline.

Image convention (matches real hardware):
    - Background: bright (200/255) — simulates backlit panel
    - Bug pixels:  dark  ( 50/255) — simulates dark insect on bright background

The pipeline uses THRESH_BINARY_INV so pixels below THRESHOLD_VALUE (120)
become the detected blobs.  The Hough circle detector falls back to the
image centre when no circular dish is found, so detections still work on
synthetic flat-background frames.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from cib.vision.pipeline import (
    run_overhead_detection,
    THRESHOLD_VALUE,
    MIN_BUG_AREA,
    MAX_BUG_AREA,
)
from cib.core.events import VisionOverheadResultEvent

# Pixel values that match the real-world imaging setup
_BG_VALUE: int = 200   # Bright backlit background
_BUG_VALUE: int = 50   # Dark bug on bright background


def _make_bright_frame(height: int = 1024, width: int = 1280) -> np.ndarray:
    """Return a uniformly bright frame (simulates backlit empty dish)."""
    return np.full((height, width), _BG_VALUE, dtype=np.uint8)


def _draw_bug(
    frame: np.ndarray,
    cx: int,
    cy: int,
    radius: int = 15,
    value: int = _BUG_VALUE,
) -> None:
    """Draw a filled dark circle onto a bright frame in-place (simulates a bug)."""
    y_idx, x_idx = np.ogrid[: frame.shape[0], : frame.shape[1]]
    mask = (x_idx - cx) ** 2 + (y_idx - cy) ** 2 <= radius ** 2
    frame[mask] = value


# ── Return type ────────────────────────────────────────────────────────────────

def test_returns_vision_event() -> None:
    """run_overhead_detection must always return a VisionOverheadResultEvent."""
    frame = _make_bright_frame()
    result = run_overhead_detection(frame)
    assert isinstance(result, VisionOverheadResultEvent)


# ── Empty frame ────────────────────────────────────────────────────────────────

def test_bright_frame_no_bugs_yields_zero() -> None:
    """A uniformly bright frame (no dark spots) should detect zero specimens."""
    frame = _make_bright_frame()
    result = run_overhead_detection(frame)
    assert result.sample_count == 0
    assert result.centroids_robot_xy == []


# ── Single sample detection ────────────────────────────────────────────────────

def test_single_bug_detected() -> None:
    """One dark spot on a bright frame should produce exactly one detection."""
    frame = _make_bright_frame()
    _draw_bug(frame, cx=640, cy=512, radius=20)
    result = run_overhead_detection(frame)
    assert result.sample_count == 1


def test_single_bug_centroid_accurate() -> None:
    """Detected centroid should be within 3 px of the drawn centre in pixel space.

    Calibration is patched to raise so the pipeline falls back to returning
    raw pixel coordinates — this isolates the detection accuracy from whatever
    homography happens to be installed on the machine running the tests.
    """
    frame = _make_bright_frame()
    cx_true, cy_true = 640, 512
    _draw_bug(frame, cx=cx_true, cy=cy_true, radius=25)

    # Force calibration to be unavailable so centroids stay in pixel space.
    # load_calibration is imported inside the function body, so patch at source.
    with patch("cib.vision.calibration.load_calibration", side_effect=FileNotFoundError):
        result = run_overhead_detection(frame)

    assert result.sample_count == 1

    cx_det, cy_det = result.centroids_robot_xy[0]
    assert abs(cx_det - cx_true) <= 3.0
    assert abs(cy_det - cy_true) <= 3.0


# ── Multiple samples ───────────────────────────────────────────────────────────

def test_three_bugs_detected() -> None:
    """Three spatially separated dark spots should each produce one detection.

    All spots are placed within 300 px of the frame centre so they fall inside
    the dish interior mask produced by the fallback dish detector, which uses
    ~40% of min(height, width) as the radius on a 1024×1280 synthetic frame.
    """
    frame = _make_bright_frame()
    # All within 300px of centre (640, 512) — safely inside the ~409px mask
    spots = [(640, 250), (400, 600), (880, 600)]
    for cx, cy in spots:
        _draw_bug(frame, cx=cx, cy=cy, radius=20)

    result = run_overhead_detection(frame)
    assert result.sample_count == 3
    assert len(result.centroids_robot_xy) == 3


# ── Area filter — upper bound ──────────────────────────────────────────────────

def test_oversized_blob_rejected() -> None:
    """A blob larger than MAX_BUG_AREA should be rejected as a merged cluster.

    Radius 200 → area ≈ 125,664 px² which well exceeds MAX_BUG_AREA (25,000).
    """
    frame = _make_bright_frame()
    _draw_bug(frame, cx=640, cy=512, radius=200)
    result = run_overhead_detection(frame)
    assert result.sample_count == 0


# ── Tiny specimen detection ────────────────────────────────────────────────────

def test_tiny_bug_detected() -> None:
    """Method C retains tiny specimens (MIN_BUG_AREA=1, no lower bound).

    A radius-5 spot (area ≈ 78 px²) must be detected.  This is the key
    behavioural difference from Methods A and B which would discard it.
    """
    frame = _make_bright_frame()
    _draw_bug(frame, cx=640, cy=512, radius=5)
    result = run_overhead_detection(frame)
    assert result.sample_count >= 1


# ── Return fields ──────────────────────────────────────────────────────────────

def test_centroid_coordinates_are_floats() -> None:
    """Centroid values must be Python floats, not ints."""
    frame = _make_bright_frame()
    _draw_bug(frame, cx=300, cy=400, radius=20)
    result = run_overhead_detection(frame)
    assert result.sample_count >= 1
    cx, cy = result.centroids_robot_xy[0]
    assert isinstance(cx, float)
    assert isinstance(cy, float)


def test_sample_count_matches_centroids_list() -> None:
    """sample_count and len(centroids_robot_xy) must always agree."""
    frame = _make_bright_frame()
    for i in range(4):
        _draw_bug(frame, cx=150 + i * 250, cy=512, radius=20)
    result = run_overhead_detection(frame)
    assert result.sample_count == len(result.centroids_robot_xy)


def test_annotated_frame_path_is_empty_string_when_no_save_dir() -> None:
    """annotated_frame_path must be an empty string when save_dir is not given."""
    frame = _make_bright_frame()
    result = run_overhead_detection(frame)
    assert hasattr(result, "annotated_frame_path")
    assert isinstance(result.annotated_frame_path, str)
    assert result.annotated_frame_path == ""


def test_bgr_frame_accepted() -> None:
    """Pipeline must accept BGR (3-channel) frames, not just grayscale."""
    gray = _make_bright_frame()
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) if False else np.stack([gray, gray, gray], axis=-1)
    _draw_bug_bgr(bgr, cx=640, cy=512, radius=20)
    result = run_overhead_detection(bgr)
    assert result.sample_count >= 1


def _draw_bug_bgr(frame: np.ndarray, cx: int, cy: int, radius: int = 15) -> None:
    """Draw a dark bug on a 3-channel BGR frame in-place."""
    y_idx, x_idx = np.ogrid[: frame.shape[0], : frame.shape[1]]
    mask = (x_idx - cx) ** 2 + (y_idx - cy) ** 2 <= radius ** 2
    frame[mask] = [_BUG_VALUE, _BUG_VALUE, _BUG_VALUE]


# ── Robustness ─────────────────────────────────────────────────────────────────

def test_all_black_frame_returns_event_not_exception() -> None:
    """A fully black frame (pathological input) must not raise — return 0 detections."""
    frame = np.zeros((1024, 1280), dtype=np.uint8)
    result = run_overhead_detection(frame)
    assert isinstance(result, VisionOverheadResultEvent)
    # Black frame → THRESH_BINARY_INV makes entire frame white → all contours
    # exceed MAX_BUG_AREA and are rejected, OR the dish mask restricts to zero.
    # Either way, no exception should propagate.


import cv2  # noqa: E402 — placed here to keep helper functions readable above
