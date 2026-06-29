#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eoa_classifier.py — EoA Camera Specimen Classification Test
============================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

End-of-Arm (EoA) binary classifier: determines whether the specimen held
by the robot arm is a real bug (go) or non-specimen material such as a
hair/filament or debris fragment (no-go).

Image convention (backlit panel, front camera):
    - Background: very bright (~235–255) — backlit white panel
    - Bug body:   dark (~20–100)          — dark insect body
    - Bug wings:  mid-grey (~150–210)     — semi-transparent wings
    - Hair:       thin filament (~100–180) on bright background
    - Debris:     small irregular blob

Algorithm
---------
1. Grayscale conversion
2. Inverse threshold at SEGMENT_THRESHOLD to isolate dark foreground
3. Morphological closing (small kernel) to bridge gaps between body/wings
4. Find all external contours ≥ MIN_CONTOUR_AREA
5. Merge overlapping/touching contours into specimen blobs
6. Select the dominant blob (largest by area near frame centre)
7. Compute shape features on dominant blob:
        aspect_ratio  = max(w, h) / max(1, min(w, h))   [bounding rect]
        solidity      = contour_area / convex_hull_area
        circularity   = 4π·area / perimeter²
        extent        = area / (w × h)
        equiv_diameter = sqrt(4·area / π)
8. Classify:
        HAIR    → aspect_ratio > HAIR_ASPECT_THRESH and solidity < HAIR_SOLIDITY_MAX
        BUG     → equiv_diameter ≥ BUG_MIN_DIAM and solidity in [BUG_SOLIDITY_MIN, BUG_SOLIDITY_MAX]
        DEBRIS  → everything else (too small, or compact smooth blob with no appendage signature)

Usage (standalone)
------------------
    python eoa_classifier.py                            # all images in default dirs
    python eoa_classifier.py path/to/image.jpg          # single image
    python eoa_classifier.py --save ./results           # batch, save annotated
    python eoa_classifier.py --no-show --save ./results # headless batch
    python eoa_classifier.py --features-only            # print feature table, no windows

Outputs saved to:  software_tests/eoa_classification/results/
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cib.eoa_classifier")

# ---------------------------------------------------------------------------
# Tunable constants — adjust based on feature-table output
# ---------------------------------------------------------------------------
# Segmentation uses Otsu's automatic threshold (THRESH_OTSU) so no fixed value is needed.
# CORNER_SAMPLE_FRAC controls what fraction of each image edge is sampled to estimate
# background brightness; used only for coverage sanity checks.
CORNER_SAMPLE_FRAC: float = 0.08    # sample 8% of each edge dimension for background est.
MAX_FG_COVERAGE: float = 0.45       # if foreground > 45% of frame → bad lighting, warn
MIN_CONTOUR_AREA: int = 50          # px² — ignore dust specks below this
CLOSE_KERNEL_SIZE: int = 15         # px — morphological close to bridge body↔wing gaps
CLOSE_ITER: int = 2

# Hair / filament / detached-leg thresholds
# Lowered aspect threshold to 1.6 so detached legs and angular fragments are caught.
# Solidity < 0.45 ensures the blob is thin relative to its convex hull.
HAIR_ASPECT_THRESH: float = 1.6  # Elongated thin shape (legs, hairs, filaments)
HAIR_SOLIDITY_MAX: float = 0.45  # Thin shape → low area/hull ratio

# Bug thresholds
BUG_MIN_DIAM: float = 25.0       # px — equivalent circle diameter, rejects tiny specks
BUG_SOLIDITY_MIN: float = 0.18   # Bugs have appendages → some concavity in convex hull
BUG_SOLIDITY_MAX: float = 0.92   # Front-light-off images produce compact silhouettes (0.85+)

# Convexity-defect appendage check
# For blobs with solidity > BUG_HISOL_THRESH the solidity alone no longer implies
# appendages, so we require at least BUG_MIN_DEFECTS deep notches (≥ MIN_DEFECT_DEPTH_PX)
# as evidence of legs/antennae/wings.  Smooth compact debris (no appendages) has zero
# significant defects and is rejected as DEBRIS.
BUG_HISOL_THRESH: float = 0.75   # Above this, solidity alone is not enough evidence
BUG_MIN_DEFECTS: int = 2          # Minimum significant convexity defects required
MIN_DEFECT_DEPTH_PX: float = 5.0  # Minimum notch depth (px) to count as appendage

# Center-first search: expanding annular rings as fractions of the frame half-diagonal.
# The EoA arm holds the specimen roughly below the camera centre.
CENTER_SEARCH_FRACS: tuple = (0.15, 0.30, 0.50, 1.0)

# ── Dark-pixel intensity gate (primary discriminator) ──────────────────────────
# Insects are melanized: dark brown/black bodies have grayscale values 20–80.
# Colored debris (reddish biological material: ~110–160) and amber hair follicles
# (~100–160) are NOT genuinely dark — they only appear dark relative to the
# bright background.
#
# Method: within the ORIGINAL binary mask (pixels segmented before morph-close),
# count the fraction with gray value < DARK_PIXEL_THRESH.
#   Bug body only:          dark_ratio ~0.8–1.0   → ACCEPT
#   Bug body + large wings: dark_ratio ~0.3–0.6   → ACCEPT
#   Reddish debris:         dark_ratio ~0.0–0.15  → REJECT
#   Amber hair follicle:    dark_ratio ~0.0–0.10  → REJECT
#
# Sampling uses the ORIGINAL binary (pre-close) so morph-close background pixels
# do not dilute the ratio for thin objects like hairs.
DARK_PIXEL_THRESH: int = 90         # Gray value below which a pixel counts as "dark"
BLOB_DARK_RATIO_MIN: float = 0.25   # Minimum dark-pixel fraction for a valid bug candidate

# BGR chroma variance: max(R,G,B) - min(R,G,B) within original-binary-masked pixels.
# Dark neutral insects: chroma ~5–20.  Colored debris / hair: chroma ~50–100.
BLOB_CHROMA_MAX: float = 45.0       # Blobs above this are colored objects, not dark insects

# ── Body-center / pipette-target constants ─────────────────────────────────────
# After EoA classification confirms BUG, we need the BODY center — not the
# geometric centroid of the whole blob (which may fall on a wing).
# Strategy: threshold the dark body pixels (gray < BODY_DARK_THRESH) inside the
# blob, find the largest connected component, then use an intensity-weighted
# centroid (weight = 255 - pixel_value) so the result is pulled toward the
# densest/darkest region (thorax/abdomen), not toward semi-transparent wings.
BODY_DARK_THRESH: int = 70   # Gray value for body-only pixels (wings: ~150-210)

# Display
DISPLAY_HEIGHT: int = 900

# Label colours (BGR)
_COLOUR = {
    "BUG":    (0, 200, 0),    # Green
    "HAIR":   (0, 165, 255),  # Orange
    "DEBRIS": (0, 0, 220),    # Red
    "NONE":   (128, 128, 128),# Grey
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BlobFeatures:
    """Shape features extracted from a single contour/blob."""
    area: float = 0.0
    perimeter: float = 0.0
    equiv_diameter: float = 0.0
    aspect_ratio: float = 0.0
    solidity: float = 0.0
    circularity: float = 0.0
    extent: float = 0.0
    centroid_x: float = 0.0
    centroid_y: float = 0.0
    # Convexity defects — points where the contour dips inward relative to the
    # convex hull.  A real bug body has deep defects at leg/antenna/wing roots;
    # smooth debris or filaments have zero or very shallow defects.
    significant_defects: int = 0        # count of defects deeper than MIN_DEFECT_DEPTH_PX
    max_defect_depth_px: float = 0.0    # depth of the deepest defect in pixels
    # Intensity features — sampled from ORIGINAL grayscale image within the
    # ORIGINAL binary mask (pre-morph-close).  This avoids diluting the signal
    # with bright background pixels added by the close operation.
    dark_pixel_ratio: float = 0.0       # fraction of pixels with gray < DARK_PIXEL_THRESH
    mean_blob_brightness: float = 255.0 # mean gray value (low = dark specimen)
    mean_chroma_variance: float = 0.0   # mean BGR chroma per pixel (high = colored debris)
    contour: Optional[np.ndarray] = field(default=None, repr=False)
    convex_hull: Optional[np.ndarray] = field(default=None, repr=False)


@dataclass
class ClassificationResult:
    """Full result for one image."""
    image_path: str
    label: str                             # "BUG" | "HAIR" | "DEBRIS" | "NONE"
    is_bug: bool
    dominant_blob: Optional[BlobFeatures]
    all_blobs: list[BlobFeatures] = field(default_factory=list)
    annotated_image: Optional[np.ndarray] = field(default=None, repr=False)
    notes: str = ""
    otsu_threshold: float = 0.0
    fg_coverage: float = 0.0              # fraction of frame that is foreground
    elapsed_ms: float = 0.0              # wall-clock time for the full pipeline
    # Pipette target: intensity-weighted centroid of dark body pixels only.
    # None when label != "BUG".  Excludes wings so the pipette hits the thorax.
    body_center_px: Optional[tuple[float, float]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    """Convert BGR image to grayscale, pass through if already single-channel."""
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _segment(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Isolate dark foreground on bright backlit background using Otsu's method.

    Otsu's binarisation automatically finds the optimal threshold between the
    background peak and the dark-specimen valley in the histogram, handling both
    bright-white-backlit panels and medium-grey-background conditions without
    any fixed threshold constant.

    Args:
        gray: Grayscale image.

    Returns:
        (binary_mask, otsu_threshold) where binary_mask has foreground = 255.
    """
    otsu_thresh, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )
    return binary, float(otsu_thresh)


def _morphological_close(binary: np.ndarray) -> np.ndarray:
    """Bridge gaps between insect body and semi-transparent wings.

    A morphological close (dilate then erode) connects nearby blobs without
    merging distant separate objects.

    Args:
        binary: Binary foreground mask.

    Returns:
        Cleaned mask with nearby blobs merged.
    """
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE)
    )
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=CLOSE_ITER)


def _extract_features(contour: np.ndarray) -> BlobFeatures:
    """Compute shape features from a single contour.

    Args:
        contour: OpenCV contour array.

    Returns:
        BlobFeatures populated with area, solidity, aspect ratio, etc.
    """
    area = cv2.contourArea(contour)
    perimeter = cv2.arcLength(contour, True)

    x, y, w, h = cv2.boundingRect(contour)
    aspect_ratio = max(w, h) / max(1, min(w, h))
    extent = area / max(1.0, float(w * h))

    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull)
    solidity = area / max(1.0, hull_area)

    circularity = 0.0
    if perimeter > 0:
        circularity = (4 * math.pi * area) / (perimeter ** 2)

    equiv_diameter = math.sqrt(4 * area / math.pi) if area > 0 else 0.0

    M = cv2.moments(contour)
    cx_f = M["m10"] / M["m00"] if M["m00"] > 0 else float(x + w / 2)
    cy_f = M["m01"] / M["m00"] if M["m00"] > 0 else float(y + h / 2)

    # Convexity defects: measure depth of inward notches relative to convex hull.
    # cv2.convexHull(returnPoints=False) gives indices; convexityDefects uses these.
    # Depth is stored as fixed-point ×256 so divide by 256.0 to get pixels.
    sig_defects = 0
    max_depth = 0.0
    if len(contour) >= 4:
        try:
            hull_idx = cv2.convexHull(contour, returnPoints=False)
            if hull_idx is not None and len(hull_idx) >= 3:
                defects = cv2.convexityDefects(contour, hull_idx)
                if defects is not None:
                    for row in defects:
                        depth_px = row[0][3] / 256.0
                        if depth_px > max_depth:
                            max_depth = depth_px
                        if depth_px >= MIN_DEFECT_DEPTH_PX:
                            sig_defects += 1
        except cv2.error:
            pass  # Degenerate contours (e.g. collinear points) raise here

    return BlobFeatures(
        area=area,
        perimeter=perimeter,
        equiv_diameter=equiv_diameter,
        aspect_ratio=aspect_ratio,
        solidity=solidity,
        circularity=circularity,
        extent=extent,
        centroid_x=cx_f,
        centroid_y=cy_f,
        significant_defects=sig_defects,
        max_defect_depth_px=max_depth,
        contour=contour,
        convex_hull=hull,
    )


def _find_blobs(binary: np.ndarray) -> list[BlobFeatures]:
    """Detect all foreground blobs above MIN_CONTOUR_AREA.

    Args:
        binary: Binary segmentation mask.

    Returns:
        List of BlobFeatures sorted by area descending.
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blobs = [
        _extract_features(c)
        for c in contours
        if cv2.contourArea(c) >= MIN_CONTOUR_AREA
    ]
    blobs.sort(key=lambda b: b.area, reverse=True)
    return blobs


def _compute_intensity_features(
    blob: BlobFeatures,
    gray: np.ndarray,
    img: np.ndarray,
    original_binary: np.ndarray,
) -> None:
    """Populate intensity/colour features on a BlobFeatures in-place.

    Samples the ORIGINAL (pre-morph-close) binary mask intersected with the
    blob's closed contour.  Using the original mask avoids diluting the signal
    with bright background pixels that the morphological close adds around thin
    objects (e.g. hair follicles).

    Features set:
        dark_pixel_ratio      — fraction of sampled pixels with gray < DARK_PIXEL_THRESH.
                                Bugs: 0.3–1.0.  Reddish debris / amber hair: 0.0–0.15.
        mean_blob_brightness  — mean grayscale value of sampled pixels.
        mean_chroma_variance  — mean per-pixel BGR chroma (max–min channel) of sampled
                                pixels.  Dark neutral insects: 5–20.
                                Colored debris / hair: 50–100.

    Args:
        blob:            BlobFeatures with a valid contour.
        gray:            Grayscale version of the original frame.
        img:             Original BGR (or grayscale) frame.
        original_binary: Binary mask from _segment() BEFORE morphological close.
    """
    if blob.contour is None:
        return

    h, w = gray.shape[:2]

    # Closed-blob mask from the contour (filled)
    closed_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(closed_mask, [blob.contour], -1, 255, -1)

    # Intersect with ORIGINAL binary so we only sample genuinely segmented pixels.
    # This removes the halo of background pixels added by morphological close.
    sample_mask = cv2.bitwise_and(original_binary, closed_mask)
    pixels_gray = gray[sample_mask > 0]

    # If original binary had no pixels inside this blob (can happen for very small
    # blobs whose morphological expansion has no original-binary overlap), fall back
    # to the full closed-blob mask.
    if len(pixels_gray) < 5:
        pixels_gray = gray[closed_mask > 0]

    if len(pixels_gray) == 0:
        return

    blob.dark_pixel_ratio = float(np.sum(pixels_gray < DARK_PIXEL_THRESH)) / len(pixels_gray)
    blob.mean_blob_brightness = float(np.mean(pixels_gray))

    # BGR chroma (per-pixel max–min channel) — only meaningful for colour images
    if img.ndim == 3:
        pixels_bgr = img[sample_mask > 0] if np.any(sample_mask) else img[closed_mask > 0]
        if len(pixels_bgr) > 0:
            chroma_per_pixel = (
                pixels_bgr.max(axis=1).astype(float) - pixels_bgr.min(axis=1).astype(float)
            )
            blob.mean_chroma_variance = float(np.mean(chroma_per_pixel))


def _rank_candidates(blobs: list[BlobFeatures], frame_shape: tuple) -> list[BlobFeatures]:
    """Return all blobs ordered by center-first priority for sequential classification.

    The EoA arm holds the specimen roughly below the camera centre.  Blobs are
    sorted into concentric rings (defined by CENTER_SEARCH_FRACS as fractions of
    the frame half-diagonal).  Within each ring, larger blobs rank first.

    The caller iterates this list and returns the FIRST blob that classifies as BUG,
    skipping any debris or hair blobs closer to centre before finding the real specimen.

    Search rings:
        1. Within 15% of half-diagonal from centre
        2. 15–30%
        3. 30–50%
        4. 50–100% (full frame fallback)

    Args:
        blobs: All detected blobs (any order).
        frame_shape: (H, W[, C]) of the original frame.

    Returns:
        All blobs in center-first, largest-first-per-ring order.  Empty if blobs is empty.
    """
    if not blobs:
        return []

    h, w = frame_shape[:2]
    cx_frame = w / 2.0
    cy_frame = h / 2.0
    half_diag = math.sqrt(cx_frame ** 2 + cy_frame ** 2)

    distances = {
        id(b): math.sqrt(
            (b.centroid_x - cx_frame) ** 2 + (b.centroid_y - cy_frame) ** 2
        )
        for b in blobs
    }

    ranked: list[BlobFeatures] = []
    prev_radius = 0.0
    for frac in CENTER_SEARCH_FRACS:
        radius = half_diag * frac
        ring_blobs = sorted(
            [b for b in blobs if prev_radius <= distances[id(b)] < radius],
            key=lambda b: b.area,
            reverse=True,
        )
        ranked.extend(ring_blobs)
        prev_radius = radius

    return ranked


def _classify_blob(blob: BlobFeatures) -> tuple[str, str]:
    """Apply rule-based classification to a blob's shape features.

    Classification pipeline (in priority order):

    1. HAIR / FILAMENT / DETACHED LEG
       Elongated thin shapes: aspect_ratio > HAIR_ASPECT_THRESH (1.6) AND
       solidity < HAIR_SOLIDITY_MAX (0.45).  Catches long hairs, detached legs,
       and angular filament fragments.

    2. Size gate — too small → DEBRIS

    3. Hard solidity ceiling — too smooth (> BUG_SOLIDITY_MAX) → DEBRIS

    4. Low-to-mid solidity [BUG_SOLIDITY_MIN, BUG_HISOL_THRESH]
       Appendage concavity already implied by the low-solidity value → BUG

    5. High solidity (BUG_HISOL_THRESH, BUG_SOLIDITY_MAX]
       Solidity alone is ambiguous (compact bug body vs. smooth debris blob).
       Require ≥ BUG_MIN_DEFECTS significant convexity defects (≥ MIN_DEFECT_DEPTH_PX
       each) as appendage evidence.  Smooth debris has 0–1 shallow defects → DEBRIS.

    Args:
        blob: Extracted features for the dominant specimen.

    Returns:
        (label, reason) where label is "BUG" | "HAIR" | "DEBRIS".
    """
    d = blob.equiv_diameter
    ar = blob.aspect_ratio
    sol = blob.solidity
    defects = blob.significant_defects
    max_d = blob.max_defect_depth_px

    # ── 1. Hair / filament / detached leg ─────────────────────────────────────
    if ar > HAIR_ASPECT_THRESH and sol < HAIR_SOLIDITY_MAX:
        return "HAIR", (
            f"aspect={ar:.2f}>{HAIR_ASPECT_THRESH} "
            f"and solidity={sol:.2f}<{HAIR_SOLIDITY_MAX} "
            f"(filament/leg/hair)"
        )

    # ── 1b. Dark-pixel intensity gate (primary physical discriminator) ─────────
    # Insects are melanized: their bodies are dark brown/black (gray 20–80).
    # Colored debris (reddish biological material) and amber hair follicles are
    # NOT genuinely dark — they segment at Otsu threshold but their original-image
    # pixels are gray 100–160.  Any blob with fewer than BLOB_DARK_RATIO_MIN truly
    # dark pixels cannot be a melanized insect.
    # Chroma check: colored objects (reddish, amber, golden) have high per-pixel
    # BGR chroma; dark neutral insects have near-zero chroma.
    if blob.dark_pixel_ratio < BLOB_DARK_RATIO_MIN:
        return "DEBRIS", (
            f"not a dark insect: dark_ratio={blob.dark_pixel_ratio:.2f} "
            f"< {BLOB_DARK_RATIO_MIN} "
            f"(brightness={blob.mean_blob_brightness:.0f}, "
            f"chroma={blob.mean_chroma_variance:.0f}) "
            f"— reddish/amber/transparent debris"
        )
    if blob.mean_chroma_variance > BLOB_CHROMA_MAX:
        return "DEBRIS", (
            f"colored object: chroma={blob.mean_chroma_variance:.0f} "
            f"> {BLOB_CHROMA_MAX} "
            f"(dark_ratio={blob.dark_pixel_ratio:.2f}) "
            f"— insect bodies are chromatically neutral"
        )

    # ── 2. Size gate ───────────────────────────────────────────────────────────
    if d < BUG_MIN_DIAM:
        return "DEBRIS", f"too small: diam={d:.0f} < {BUG_MIN_DIAM:.0f}px"

    # ── 3. Hard solidity ceiling ───────────────────────────────────────────────
    if sol > BUG_SOLIDITY_MAX:
        return "DEBRIS", (
            f"solidity={sol:.2f} > {BUG_SOLIDITY_MAX} "
            f"(perfectly smooth blob, no appendages possible)"
        )

    # ── 4. Below minimum solidity ──────────────────────────────────────────────
    if sol < BUG_SOLIDITY_MIN:
        return "DEBRIS", f"solidity={sol:.2f} < {BUG_SOLIDITY_MIN} (too fragmented)"

    # ── 5a. Mid solidity: appendages implied ───────────────────────────────────
    if sol <= BUG_HISOL_THRESH:
        return "BUG", (
            f"diam={d:.0f} solid={sol:.2f} in appendage range "
            f"[{BUG_SOLIDITY_MIN},{BUG_HISOL_THRESH}] "
            f"(defects={defects} max_depth={max_d:.1f}px)"
        )

    # ── 5b. High solidity: require appendage evidence from defects ─────────────
    # sol is in (BUG_HISOL_THRESH, BUG_SOLIDITY_MAX]
    if defects >= BUG_MIN_DEFECTS:
        return "BUG", (
            f"compact body: solid={sol:.2f} with "
            f"{defects} appendage notches (max {max_d:.1f}px deep)"
        )

    return "DEBRIS", (
        f"smooth compact blob: solid={sol:.2f} > {BUG_HISOL_THRESH} "
        f"but only {defects} defect(s) < {BUG_MIN_DEFECTS} required "
        f"(max_depth={max_d:.1f}px) — no appendage signature"
    )


def find_body_center(
    blob: BlobFeatures,
    gray: np.ndarray,
    original_binary: np.ndarray,
) -> tuple[float, float, np.ndarray]:
    """Find the intensity-weighted center of mass of the bug body, excluding wings.

    Wings are semi-transparent (gray ~150–210) and must NOT be the pipette target —
    picking from a wing risks breaking it.  This function isolates only the dark
    body pixels (gray < BODY_DARK_THRESH) within the blob and computes their
    intensity-weighted centroid.

    Weighting: weight = (255 - pixel_gray) — darker pixels are heavier, so the
    result is pulled toward the densest part of the body (thorax/abdomen) rather
    than the geometric centre of the whole blob.

    If body pixels cannot be isolated (e.g. all-dark blob with no wing contrast),
    falls back to the full dark-pixel centroid of the blob.

    Args:
        blob:            BlobFeatures with a valid contour.
        gray:            Grayscale version of the original frame.
        original_binary: Binary mask from _segment() BEFORE morphological close.

    Returns:
        (cx, cy, body_mask) where (cx, cy) are pixel coordinates of the pipette
        target and body_mask is a uint8 binary image of the isolated body region.
    """
    if blob.contour is None:
        return blob.centroid_x, blob.centroid_y, np.zeros(gray.shape[:2], dtype=np.uint8)

    h, w = gray.shape[:2]

    # Full closed-blob mask from the classified contour
    closed_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(closed_mask, [blob.contour], -1, 255, -1)

    # Use ORIGINAL binary pixels inside the blob to avoid morph-close halo
    sample_mask = cv2.bitwise_and(original_binary, closed_mask)
    if np.count_nonzero(sample_mask) < 5:
        sample_mask = closed_mask  # fallback: use full closed mask

    # Isolate body pixels with tighter threshold than DARK_PIXEL_THRESH
    _, body_binary = cv2.threshold(gray, BODY_DARK_THRESH, 255, cv2.THRESH_BINARY_INV)
    body_mask = cv2.bitwise_and(body_binary, sample_mask)

    # If body threshold is too tight (e.g. pale specimen), relax to sample mask
    if np.count_nonzero(body_mask) < 10:
        log.debug("  find_body_center: body threshold too tight, using full sample mask")
        body_mask = sample_mask.copy()

    # Find the largest connected body component to avoid detached legs or antennae
    # pulling the centroid away from the thorax
    n_labels, labels, stats, centroids_cc = cv2.connectedComponentsWithStats(
        body_mask, connectivity=8
    )

    if n_labels <= 1:
        # No connected component found — fall back to blob centroid
        return blob.centroid_x, blob.centroid_y, body_mask

    # Skip label 0 (background), pick the largest component by pixel area
    largest_label = int(1 + np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    body_component = ((labels == largest_label).astype(np.uint8)) * 255

    # Intensity-weighted centroid: weight = 255 - gray → darker = heavier
    ys, xs = np.where(body_component > 0)
    if len(xs) == 0:
        return blob.centroid_x, blob.centroid_y, body_mask

    weights = 255.0 - gray[ys, xs].astype(float)
    total_w = float(weights.sum())
    if total_w > 0:
        cx = float(np.dot(xs.astype(float), weights) / total_w)
        cy = float(np.dot(ys.astype(float), weights) / total_w)
    else:
        # All pixels equally dark — use geometric centroid of component
        cx = float(centroids_cc[largest_label][0])
        cy = float(centroids_cc[largest_label][1])

    return cx, cy, body_component


def _annotate(
    img: np.ndarray,
    result: ClassificationResult,
    dominant: BlobFeatures,
    body_center: Optional[tuple[float, float]] = None,
    body_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Draw classification result onto the image.

    Draws:
        - Orange convex hull of dominant blob
        - Coloured contour of dominant blob
        - Red dot: blob geometric centroid
        - Cyan overlay: isolated body pixels (when body_mask provided)
        - Cyan crosshair + circle: pipette target (body center, when provided)
        - Top-left banner: label + key features + pipette target coords

    Args:
        img:         Original BGR or grayscale image.
        result:      Classification result.
        dominant:    Features of the dominant blob.
        body_center: (cx, cy) pipette target pixel coords, or None.
        body_mask:   Binary mask of body pixels for overlay, or None.

    Returns:
        Annotated BGR image.
    """
    out = img.copy() if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    colour = _COLOUR.get(result.label, _COLOUR["NONE"])

    # Draw all non-dominant blobs in grey
    for blob in result.all_blobs:
        if blob is dominant:
            continue
        if blob.contour is not None:
            cv2.drawContours(out, [blob.contour], -1, (180, 180, 180), 1)

    # Body pixel overlay in semi-transparent cyan (BUG only)
    if body_mask is not None and np.any(body_mask):
        overlay = out.copy()
        overlay[body_mask > 0] = (255, 220, 0)   # cyan-ish (BGR: yellow-cyan)
        cv2.addWeighted(overlay, 0.35, out, 0.65, 0, out)

    # Draw dominant contour
    if dominant.contour is not None:
        cv2.drawContours(out, [dominant.contour], -1, colour, 2)

    # Draw convex hull in orange
    if dominant.convex_hull is not None:
        cv2.polylines(out, [dominant.convex_hull], True, (0, 165, 255), 2)

    # Blob geometric centroid (red dot)
    cx_i = int(round(dominant.centroid_x))
    cy_i = int(round(dominant.centroid_y))
    cv2.circle(out, (cx_i, cy_i), 6, (0, 0, 255), -1)

    # Pipette target: body center crosshair (cyan) — distinct from centroid
    if body_center is not None:
        bcx = int(round(body_center[0]))
        bcy = int(round(body_center[1]))
        arm = max(18, int(dominant.equiv_diameter * 0.25))
        lw  = max(2, int(dominant.equiv_diameter * 0.03))
        # Crosshair lines
        cv2.line(out, (bcx - arm, bcy), (bcx + arm, bcy), (255, 255, 0), lw)
        cv2.line(out, (bcx, bcy - arm), (bcx, bcy + arm), (255, 255, 0), lw)
        # Targeting circle
        cv2.circle(out, (bcx, bcy), arm // 2, (255, 255, 0), lw)
        # Small filled dot at exact target
        cv2.circle(out, (bcx, bcy), 4, (0, 255, 255), -1)

    # Banner text
    lines = [
        f"Label: {result.label}",
        f"area={dominant.area:.0f}px2  diam={dominant.equiv_diameter:.0f}px",
        f"aspect={dominant.aspect_ratio:.2f}  solid={dominant.solidity:.2f}",
        f"defects={dominant.significant_defects}  maxD={dominant.max_defect_depth_px:.1f}px",
    ]
    if body_center is not None:
        lines.append(f"pipette target: ({body_center[0]:.1f}, {body_center[1]:.1f}) px")

    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.5, img.shape[1] / 1800)
    thick = max(1, int(scale * 2))
    line_h = int(cv2.getTextSize("A", font, scale, thick)[0][1] * 2.2)
    pad = 10

    panel_h = pad * 2 + line_h * len(lines)
    panel_w = int(img.shape[1] * 0.58)
    cv2.rectangle(out, (0, 0), (panel_w, panel_h), (20, 20, 20), -1)

    for i, text in enumerate(lines):
        y = pad + (i + 1) * line_h
        txt_colour = colour if i == 0 else (220, 220, 220)
        cv2.putText(out, text, (pad, y), font, scale, txt_colour, thick)

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_eoa_image(
    image_path: Path | str,
    save_dir: Optional[Path] = None,
) -> ClassificationResult:
    """Run the full EoA classification pipeline on one image.

    Args:
        image_path: Path to a backlit-panel EoA image (JPG or PNG).
        save_dir: If given, write the annotated image here.

    Returns:
        ClassificationResult with label, features, and optional annotated image.
        On error, result.error is set and label is "NONE".
    """
    image_path = Path(image_path)
    log.info("EoA classify | %s", image_path.name)

    t_start = time.perf_counter()

    try:
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(f"cv2.imread returned None: {image_path}")

        gray = _to_gray(img)
        binary, otsu_thresh = _segment(gray)
        log.info("  Otsu threshold: %.0f", otsu_thresh)

        # Sanity check: if too much of the frame is foreground the lighting is unusable.
        # Backlight-off images produce dark frames where Otsu maps almost everything to
        # foreground — classification on such frames is meaningless.
        h, w = gray.shape[:2]
        frame_area = float(h * w)
        fg_coverage = float(np.count_nonzero(binary)) / frame_area
        if fg_coverage > MAX_FG_COVERAGE:
            log.warning(
                "  Foreground covers %.0f%% of frame (> %.0f%%) — "
                "backlight may be off or image lacks contrast. Classification aborted.",
                fg_coverage * 100, MAX_FG_COVERAGE * 100,
            )
            return ClassificationResult(
                image_path=str(image_path),
                label="NONE",
                is_bug=False,
                dominant_blob=None,
                notes="Bad lighting: foreground coverage too high — ensure backlight is ON",
                otsu_threshold=otsu_thresh,
                fg_coverage=fg_coverage,
                elapsed_ms=(time.perf_counter() - t_start) * 1000,
            )

        closed = _morphological_close(binary)
        blobs = _find_blobs(closed)

        log.info("  Blobs found: %d (above %d px^2)", len(blobs), MIN_CONTOUR_AREA)

        if not blobs:
            elapsed = (time.perf_counter() - t_start) * 1000
            result = ClassificationResult(
                image_path=str(image_path),
                label="NONE",
                is_bug=False,
                dominant_blob=None,
                all_blobs=[],
                notes="No foreground blobs detected",
                otsu_threshold=otsu_thresh,
                fg_coverage=fg_coverage,
                elapsed_ms=elapsed,
            )
            log.info("  -> NONE (no blobs)")
            return result

        # Rank all blobs in center-first priority order (innermost ring first,
        # largest-first within each ring), then iterate until we find a BUG.
        # This means a tiny debris speck near center doesn't block a real bug
        # at a slightly larger radius from being found.
        candidates = _rank_candidates(blobs, gray.shape)

        dominant = candidates[0]   # default: closest-to-center blob (for NONE display)
        label = "NONE"
        reason = "no blob in image passed bug criteria"

        for candidate in candidates:
            _compute_intensity_features(candidate, gray, img, binary)
            cand_label, cand_reason = _classify_blob(candidate)
            log.info(
                "  Candidate: area=%.0f diam=%.0f aspect=%.2f solid=%.2f "
                "dark_ratio=%.2f chroma=%.0f -> %s",
                candidate.area, candidate.equiv_diameter,
                candidate.aspect_ratio, candidate.solidity,
                candidate.dark_pixel_ratio, candidate.mean_chroma_variance,
                cand_label,
            )
            if cand_label == "BUG":
                dominant = candidate
                label = "BUG"
                reason = cand_reason
                break
            # Keep looking — this candidate is HAIR/DEBRIS, try the next one.
            # Record the first candidate's reason as fallback note.
            if candidate is candidates[0]:
                reason = f"center blob rejected ({cand_reason}); searching further"

        log.info("  -> %s | %s", label, reason)

        # ── Body center / pipette target ───────────────────────────────────────
        # Only computed when a real bug is confirmed.  Uses a tighter threshold
        # (BODY_DARK_THRESH < DARK_PIXEL_THRESH) so that semi-transparent wings
        # are excluded and the pipette targets the dense thorax/abdomen region.
        body_center: Optional[tuple[float, float]] = None
        body_mask_out: Optional[np.ndarray] = None
        if label == "BUG":
            bc_x, bc_y, body_mask_out = find_body_center(dominant, gray, binary)
            body_center = (bc_x, bc_y)
            log.info(
                "  Body center (pipette target): (%.1f, %.1f) px", bc_x, bc_y
            )

        elapsed = (time.perf_counter() - t_start) * 1000
        log.info("  Elapsed: %.1f ms", elapsed)

        annotated = _annotate(
            img,
            ClassificationResult(
                image_path=str(image_path), label=label, is_bug=(label == "BUG"),
                dominant_blob=dominant,
            ),
            dominant,
            body_center=body_center,
            body_mask=body_mask_out,
        )

        result = ClassificationResult(
            image_path=str(image_path),
            label=label,
            is_bug=(label == "BUG"),
            dominant_blob=dominant,
            all_blobs=blobs,
            annotated_image=annotated,
            notes=reason,
            otsu_threshold=otsu_thresh,
            fg_coverage=fg_coverage,
            elapsed_ms=elapsed,
            body_center_px=body_center,
        )

        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            out_name = f"eoa_{image_path.stem}_{label}{image_path.suffix}"
            out_path = save_dir / out_name
            cv2.imwrite(str(out_path), annotated)
            log.info("  Saved: %s", out_path.name)

        return result

    except FileNotFoundError as exc:
        log.error("File not found: %s", exc)
        return ClassificationResult(
            image_path=str(image_path), label="NONE", is_bug=False,
            dominant_blob=None, error=str(exc),
            elapsed_ms=(time.perf_counter() - t_start) * 1000,
        )
    except cv2.error as exc:
        log.error("OpenCV error on %s: %s", image_path.name, exc)
        return ClassificationResult(
            image_path=str(image_path), label="NONE", is_bug=False,
            dominant_blob=None, error=f"OpenCV: {exc}",
            elapsed_ms=(time.perf_counter() - t_start) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error on %s", image_path.name)
        return ClassificationResult(
            image_path=str(image_path), label="NONE", is_bug=False,
            dominant_blob=None, error=str(exc),
            elapsed_ms=(time.perf_counter() - t_start) * 1000,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

# Default image directories (relative to this file's location in the repo)
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_DEFAULT_DIRS: list[Path] = [
    _REPO_ROOT.parent / "Images",
    _REPO_ROOT.parent / "EOA_images",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EoA Specimen Classifier — CIB001",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Classifies EoA camera images as BUG / HAIR / DEBRIS.\n"
            "Requires backlit-panel images (bright background, dark specimen)."
        ),
    )
    parser.add_argument(
        "input", nargs="?", default=None,
        help="Image file or folder (default: Software/Images + EOA_images dirs)",
    )
    parser.add_argument(
        "--save", "-s", default=None,
        help="Directory to save annotated images (default: ./results/)",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Skip live display window (headless / batch mode).",
    )
    parser.add_argument(
        "--features-only", action="store_true",
        help="Print feature table only — do not open display windows.",
    )
    return parser.parse_args()


def _collect_paths(input_arg: Optional[str]) -> list[Path]:
    """Resolve the list of image paths to process."""
    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}

    if input_arg:
        p = Path(input_arg)
        if p.is_file():
            return [p]
        if p.is_dir():
            return sorted(f for f in p.iterdir() if f.suffix.lower() in _IMG_EXTS)
        log.error("'%s' is not a valid file or directory.", input_arg)
        sys.exit(1)

    paths: list[Path] = []
    for d in _DEFAULT_DIRS:
        if d.exists():
            found = sorted(f for f in d.iterdir() if f.suffix.lower() in _IMG_EXTS)
            paths.extend(found)
            log.info("Found %d image(s) in %s", len(found), d)
        else:
            log.warning("Default image directory not found: %s", d)

    return paths


def main() -> None:
    """CLI entry point: classify one image or a whole folder."""
    args = _parse_args()
    paths = _collect_paths(args.input)

    if not paths:
        log.error("No images found. Pass an image path or folder as argument.")
        sys.exit(1)

    save_dir = Path(args.save) if args.save else _SCRIPT_DIR / "results"
    headless = args.no_show or args.features_only

    print(f"\n{'=' * 80}")
    print(f"  EoA Specimen Classifier  |  {len(paths)} image(s)")
    print(f"  Thresholds: Otsu auto-threshold  hair_ar>{HAIR_ASPECT_THRESH}"
          f"  hair_sol<{HAIR_SOLIDITY_MAX}  bug_diam>={BUG_MIN_DIAM:.0f}")
    print(f"{'=' * 80}")
    print(
        f"{'Image':<35} {'Label':<8} {'Otsu':>5} {'FGcov':>6} {'Area':>8} "
        f"{'Diam':>6} {'AspRt':>6} {'Solid':>6} {'DrkRt':>6} {'Brt':>5} {'Chr':>5} {'ms':>6}"
    )
    print("-" * 105)

    results: list[ClassificationResult] = []
    for p in paths:
        r = classify_eoa_image(p, save_dir=save_dir if not args.features_only else None)
        results.append(r)

        otsu_str = f"{r.otsu_threshold:>5.0f}"
        cov_str  = f"{r.fg_coverage * 100:>5.1f}%"

        ms_str = f"{r.elapsed_ms:>6.1f}"

        if r.error:
            print(f"{p.name:<35} {'ERROR':<8} {otsu_str} {cov_str}  {r.error}  {ms_str}ms")
            continue

        d = r.dominant_blob
        if d is None:
            print(f"{p.name:<35} {'NONE':<8} {otsu_str} {cov_str}  {ms_str}ms")
            continue

        warn = " !" if r.fg_coverage > MAX_FG_COVERAGE else ""
        print(
            f"{p.name:<35} {r.label:<8} {otsu_str} {cov_str} "
            f"{d.area:>8.0f} {d.equiv_diameter:>6.1f} "
            f"{d.aspect_ratio:>6.2f} {d.solidity:>6.2f} "
            f"{d.dark_pixel_ratio:>6.2f} {d.mean_blob_brightness:>5.0f} "
            f"{d.mean_chroma_variance:>5.0f} {ms_str}{warn}"
        )

        if not headless and r.annotated_image is not None:
            scale = DISPLAY_HEIGHT / r.annotated_image.shape[0]
            disp = cv2.resize(
                r.annotated_image,
                (int(r.annotated_image.shape[1] * scale), DISPLAY_HEIGHT),
            )
            cv2.imshow(f"EoA — {p.name}  [any key = next]", disp)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

    # Summary
    bugs    = sum(1 for r in results if r.label == "BUG")
    hairs   = sum(1 for r in results if r.label == "HAIR")
    debris  = sum(1 for r in results if r.label == "DEBRIS")
    none_c  = sum(1 for r in results if r.label == "NONE")
    errors  = sum(1 for r in results if r.error)

    total_ms = sum(r.elapsed_ms for r in results)
    avg_ms = total_ms / len(results) if results else 0.0

    print(f"\n{'=' * 80}")
    print(f"  Images : {len(paths)}")
    print(f"  BUG    : {bugs}")
    print(f"  HAIR   : {hairs}")
    print(f"  DEBRIS : {debris}")
    print(f"  NONE   : {none_c}")
    print(f"  ERRORS : {errors}")
    print(f"  Time   : {avg_ms:.1f} ms/image avg  ({total_ms:.0f} ms total)")
    if not args.features_only:
        print(f"  Saved  : {save_dir}")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()
