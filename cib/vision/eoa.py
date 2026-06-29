"""
cib/vision/eoa.py -- EoA (End-of-Arm) camera vision analysis.

Classifies the specimen held by the robot arm and returns the precise
body-center pipette target in robot coordinate space.

Why body center, not blob centroid
-----------------------------------
The overhead camera gives a rough centroid that may fall on a wing.
The EoA camera is close-up and sees the specimen clearly. We isolate
only the dark body pixels (gray < BODY_DARK_THRESH = 70, wings are
~150-210), compute their intensity-weighted centroid, and use that as
the exact pipette drop point. This prevents picking from a wing and
breaking it.

Pipeline
--------
1.  Grayscale + Otsu inverse threshold -> foreground mask
2.  Coverage guard: >45% foreground = bad lighting -> no-go
3.  Morphological close -> bridge body/wing gaps
4.  Find all contour blobs
5.  Rank blobs center-first (arm holds specimen below camera centre)
6.  Iterate candidates; skip HAIR/DEBRIS; stop at first BUG
7.  If BUG: intensity-weighted body-center of dark pixels only
8.  Transform body-center pixel -> robot XY mm via EoA calibration
    (if calibration absent, refined_x/y = None; orchestrator falls
     back to the overhead centroid -- same behaviour as before)
9.  Optionally save annotated frame; return VisionEoAResultEvent

Algorithm constants are identical to the tuned values in:
    software_tests/eoa_classification/eoa_classifier.py
If you change a threshold here, update it there too (and vice versa).
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import structlog

from cib.core.events import VisionEoAResultEvent

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants -- must stay in sync with eoa_classifier.py
# ---------------------------------------------------------------------------

MAX_FG_COVERAGE: float  = 0.45   # >45% foreground = backlight off or bad image
MIN_CONTOUR_AREA: int   = 50     # px^2 -- ignore sub-pixel dust
CLOSE_KERNEL_SIZE: int  = 15     # morphological close kernel (bridges body/wing)
CLOSE_ITER: int         = 2

HAIR_ASPECT_THRESH: float  = 1.6   # elongated thin shapes (legs, hairs, filaments)
HAIR_SOLIDITY_MAX: float   = 0.45

BUG_MIN_DIAM: float        = 25.0   # px -- equiv circle diameter
BUG_SOLIDITY_MIN: float    = 0.18
BUG_SOLIDITY_MAX: float    = 0.92
BUG_HISOL_THRESH: float    = 0.75   # above this: need convexity defects evidence
BUG_MIN_DEFECTS: int       = 2
MIN_DEFECT_DEPTH_PX: float = 5.0

CENTER_SEARCH_FRACS: tuple = (0.15, 0.30, 0.50, 1.0)  # expanding annular rings

DARK_PIXEL_THRESH: int     = 90    # gray < 90 = genuinely dark (bug body/abdomen)
BLOB_DARK_RATIO_MIN: float = 0.25  # fraction of dark pixels required for bug
BLOB_CHROMA_MAX: float     = 45.0  # colored objects (reddish, amber) rejected

BODY_DARK_THRESH: int      = 70    # tighter: isolates body only, excludes wings


# ---------------------------------------------------------------------------
# Internal segmentation helpers
# ---------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _segment(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Otsu inverse threshold -- auto-adapts to any backlight level."""
    thresh, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )
    return binary, float(thresh)


def _morph_close(binary: np.ndarray) -> np.ndarray:
    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (CLOSE_KERNEL_SIZE, CLOSE_KERNEL_SIZE)
    )
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=CLOSE_ITER)


def _blob_features(contour: np.ndarray) -> dict:
    """Return a dict of shape features for one contour."""
    area = cv2.contourArea(contour)
    perim = cv2.arcLength(contour, True)
    x, y, w, h = cv2.boundingRect(contour)
    ar = max(w, h) / max(1, min(w, h))
    hull = cv2.convexHull(contour)
    hull_area = max(1.0, cv2.contourArea(hull))
    sol = area / hull_area
    circ = (4 * math.pi * area) / (perim ** 2) if perim > 0 else 0.0
    diam = math.sqrt(4 * area / math.pi) if area > 0 else 0.0
    M = cv2.moments(contour)
    cx = M["m10"] / M["m00"] if M["m00"] > 0 else float(x + w / 2)
    cy = M["m01"] / M["m00"] if M["m00"] > 0 else float(y + h / 2)

    sig_defects = 0
    max_depth = 0.0
    if len(contour) >= 4:
        try:
            hull_idx = cv2.convexHull(contour, returnPoints=False)
            if hull_idx is not None and len(hull_idx) >= 3:
                defects = cv2.convexityDefects(contour, hull_idx)
                if defects is not None:
                    for row in defects:
                        d = row[0][3] / 256.0
                        max_depth = max(max_depth, d)
                        if d >= MIN_DEFECT_DEPTH_PX:
                            sig_defects += 1
        except cv2.error:
            pass

    return dict(
        area=area, perimeter=perim, equiv_diameter=diam, aspect_ratio=ar,
        solidity=sol, circularity=circ,
        centroid_x=cx, centroid_y=cy,
        significant_defects=sig_defects, max_defect_depth_px=max_depth,
        contour=contour, convex_hull=hull,
        # Intensity features populated later
        dark_pixel_ratio=0.0, mean_blob_brightness=255.0, mean_chroma_variance=0.0,
    )


def _find_blobs(binary: np.ndarray) -> list[dict]:
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    blobs = [
        _blob_features(c)
        for c in contours
        if cv2.contourArea(c) >= MIN_CONTOUR_AREA
    ]
    blobs.sort(key=lambda b: b["area"], reverse=True)
    return blobs


def _intensity_features(
    blob: dict,
    gray: np.ndarray,
    img: np.ndarray,
    original_binary: np.ndarray,
) -> None:
    """Populate dark_pixel_ratio, mean_blob_brightness, mean_chroma_variance in-place.

    Samples from ORIGINAL binary (pre-morph-close) to avoid diluting the signal
    with background pixels that morphological close adds around thin objects.
    """
    h, w = gray.shape[:2]
    closed_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(closed_mask, [blob["contour"]], -1, 255, -1)

    sample_mask = cv2.bitwise_and(original_binary, closed_mask)
    pixels_gray = gray[sample_mask > 0]
    if len(pixels_gray) < 5:
        pixels_gray = gray[closed_mask > 0]
    if len(pixels_gray) == 0:
        return

    blob["dark_pixel_ratio"] = float(
        np.sum(pixels_gray < DARK_PIXEL_THRESH) / len(pixels_gray)
    )
    blob["mean_blob_brightness"] = float(np.mean(pixels_gray))

    if img.ndim == 3:
        px_bgr = img[sample_mask > 0] if np.any(sample_mask) else img[closed_mask > 0]
        if len(px_bgr) > 0:
            chroma = px_bgr.max(axis=1).astype(float) - px_bgr.min(axis=1).astype(float)
            blob["mean_chroma_variance"] = float(np.mean(chroma))


def _rank_candidates(
    blobs: list[dict], frame_shape: tuple
) -> list[dict]:
    """Return all blobs ordered center-first, largest-first within each ring."""
    if not blobs:
        return []
    h, w = frame_shape[:2]
    cx_f, cy_f = w / 2.0, h / 2.0
    half_diag = math.sqrt(cx_f ** 2 + cy_f ** 2)
    distances = {
        id(b): math.sqrt((b["centroid_x"] - cx_f) ** 2 + (b["centroid_y"] - cy_f) ** 2)
        for b in blobs
    }
    ranked: list[dict] = []
    prev_r = 0.0
    for frac in CENTER_SEARCH_FRACS:
        r = half_diag * frac
        ring = sorted(
            [b for b in blobs if prev_r <= distances[id(b)] < r],
            key=lambda b: b["area"], reverse=True,
        )
        ranked.extend(ring)
        prev_r = r
    return ranked


def _classify(blob: dict) -> tuple[str, str]:
    """Return (label, reason). Labels: 'BUG' | 'HAIR' | 'DEBRIS'."""
    d   = blob["equiv_diameter"]
    ar  = blob["aspect_ratio"]
    sol = blob["solidity"]
    df  = blob["significant_defects"]
    md  = blob["max_defect_depth_px"]
    dratio = blob["dark_pixel_ratio"]
    chroma = blob["mean_chroma_variance"]
    brt    = blob["mean_blob_brightness"]

    # 1. Hair / filament / detached leg
    if ar > HAIR_ASPECT_THRESH and sol < HAIR_SOLIDITY_MAX:
        return "HAIR", f"aspect={ar:.2f}>{HAIR_ASPECT_THRESH} sol={sol:.2f}<{HAIR_SOLIDITY_MAX}"

    # 2. Dark-pixel gate: insects are melanized (gray 20-80); colored debris is not
    if dratio < BLOB_DARK_RATIO_MIN:
        return "DEBRIS", (
            f"dark_ratio={dratio:.2f}<{BLOB_DARK_RATIO_MIN} "
            f"brt={brt:.0f} chroma={chroma:.0f} -- not a dark insect"
        )
    # 3. Chroma gate: colored objects (reddish, amber) rejected
    if chroma > BLOB_CHROMA_MAX:
        return "DEBRIS", f"chroma={chroma:.0f}>{BLOB_CHROMA_MAX} -- colored, not an insect"

    # 4. Size gate
    if d < BUG_MIN_DIAM:
        return "DEBRIS", f"diam={d:.0f}<{BUG_MIN_DIAM:.0f}px"

    # 5. Solidity ceiling
    if sol > BUG_SOLIDITY_MAX:
        return "DEBRIS", f"sol={sol:.2f}>{BUG_SOLIDITY_MAX} -- perfectly smooth, no appendages"

    # 6. Solidity floor
    if sol < BUG_SOLIDITY_MIN:
        return "DEBRIS", f"sol={sol:.2f}<{BUG_SOLIDITY_MIN} -- too fragmented"

    # 7. Mid solidity: appendages implied by concavity
    if sol <= BUG_HISOL_THRESH:
        return "BUG", (
            f"diam={d:.0f} sol={sol:.2f} in [{BUG_SOLIDITY_MIN},{BUG_HISOL_THRESH}] "
            f"defects={df} maxD={md:.1f}px"
        )

    # 8. High solidity: need defect evidence
    if df >= BUG_MIN_DEFECTS:
        return "BUG", f"compact body sol={sol:.2f} with {df} appendage notches (max {md:.1f}px)"

    return "DEBRIS", (
        f"smooth compact sol={sol:.2f}>{BUG_HISOL_THRESH} "
        f"only {df} defect(s)<{BUG_MIN_DEFECTS} required"
    )


# ---------------------------------------------------------------------------
# Body center -- the pipette target
# ---------------------------------------------------------------------------

def _body_center(
    blob: dict,
    gray: np.ndarray,
    original_binary: np.ndarray,
) -> tuple[float, float, np.ndarray]:
    """Intensity-weighted centroid of the dark body pixels (excludes wings).

    Wings are semi-transparent (~150-210 gray). By thresholding at
    BODY_DARK_THRESH=70 we keep only the dense thorax/abdomen pixels.
    Weight = (255 - pixel_gray) so the darkest part of the body pulls
    the centroid -- the pipette targets the thickest, safest region.

    Returns (cx_px, cy_px, body_mask_uint8).
    Falls back to blob centroid if body pixels cannot be isolated.
    """
    contour = blob["contour"]
    h, w = gray.shape[:2]

    closed_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(closed_mask, [contour], -1, 255, -1)

    sample_mask = cv2.bitwise_and(original_binary, closed_mask)
    if np.count_nonzero(sample_mask) < 5:
        sample_mask = closed_mask

    _, body_bin = cv2.threshold(gray, BODY_DARK_THRESH, 255, cv2.THRESH_BINARY_INV)
    body_mask = cv2.bitwise_and(body_bin, sample_mask)
    if np.count_nonzero(body_mask) < 10:
        body_mask = sample_mask.copy()

    n_labels, labels, stats, centroids_cc = cv2.connectedComponentsWithStats(
        body_mask, connectivity=8
    )
    if n_labels <= 1:
        return blob["centroid_x"], blob["centroid_y"], body_mask

    largest = int(1 + np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    component = ((labels == largest).astype(np.uint8)) * 255

    ys, xs = np.where(component > 0)
    if len(xs) == 0:
        return blob["centroid_x"], blob["centroid_y"], body_mask

    weights = 255.0 - gray[ys, xs].astype(float)
    total_w = weights.sum()
    if total_w > 0:
        cx = float(np.dot(xs.astype(float), weights) / total_w)
        cy = float(np.dot(ys.astype(float), weights) / total_w)
    else:
        cx, cy = float(centroids_cc[largest][0]), float(centroids_cc[largest][1])

    return cx, cy, component


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

_COLOUR_BUG  = (0, 200,   0)   # green
_COLOUR_NONE = (128, 128, 128) # grey


def _annotate(
    img: np.ndarray,
    blob: dict,
    label: str,
    body_center: Optional[tuple[float, float]],
    body_mask: Optional[np.ndarray],
    elapsed_ms: float,
) -> np.ndarray:
    """Draw EoA result onto a copy of the frame for offline review."""
    out = img.copy() if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    colour = _COLOUR_BUG if label == "BUG" else _COLOUR_NONE

    # Body pixel overlay (cyan-tinted)
    if body_mask is not None and np.any(body_mask):
        overlay = out.copy()
        overlay[body_mask > 0] = (255, 220, 0)
        cv2.addWeighted(overlay, 0.35, out, 0.65, 0, out)

    # Blob contour
    if blob.get("contour") is not None:
        cv2.drawContours(out, [blob["contour"]], -1, colour, 2)
    if blob.get("convex_hull") is not None:
        cv2.polylines(out, [blob["convex_hull"]], True, (0, 165, 255), 2)

    # Blob centroid (red dot)
    cv2.circle(out, (int(blob["centroid_x"]), int(blob["centroid_y"])), 6, (0, 0, 255), -1)

    # Body center crosshair (cyan) -- the pipette target
    if body_center is not None:
        bcx, bcy = int(round(body_center[0])), int(round(body_center[1]))
        arm = max(18, int(blob["equiv_diameter"] * 0.25))
        lw  = max(2, int(blob["equiv_diameter"] * 0.03))
        cv2.line(out, (bcx - arm, bcy), (bcx + arm, bcy), (255, 255, 0), lw)
        cv2.line(out, (bcx, bcy - arm), (bcx, bcy + arm), (255, 255, 0), lw)
        cv2.circle(out, (bcx, bcy), arm // 2, (255, 255, 0), lw)
        cv2.circle(out, (bcx, bcy), 4, (0, 255, 255), -1)

    # Banner
    lines = [
        f"EoA: {label}",
        f"diam={blob['equiv_diameter']:.0f}px  sol={blob['solidity']:.2f}",
        f"dark_ratio={blob['dark_pixel_ratio']:.2f}  chroma={blob['mean_chroma_variance']:.0f}",
    ]
    if body_center is not None:
        lines.append(f"pipette px: ({body_center[0]:.1f}, {body_center[1]:.1f})")
    lines.append(f"t={elapsed_ms:.0f}ms")

    font  = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.5, out.shape[1] / 1800)
    thick = max(1, int(scale * 2))
    lh    = int(cv2.getTextSize("A", font, scale, thick)[0][1] * 2.2)
    pad   = 10
    pw    = int(out.shape[1] * 0.55)
    ph    = pad * 2 + lh * len(lines)
    cv2.rectangle(out, (0, 0), (pw, ph), (20, 20, 20), -1)
    for i, text in enumerate(lines):
        tc = colour if i == 0 else (220, 220, 220)
        cv2.putText(out, text, (pad, pad + (i + 1) * lh), font, scale, tc, thick)

    return out


# ---------------------------------------------------------------------------
# Public API -- called by the orchestrator via run_in_executor
# ---------------------------------------------------------------------------

def run_eoa_analysis(
    frame: np.ndarray,
    save_dir: Optional[Path] = None,
) -> VisionEoAResultEvent:
    """Capture and save EoA frame. Always proceeds — classification comes later.

    Args:
        frame:    BGR numpy array from the EoA camera (ring light on, backlight off).
        save_dir: If provided, write a JPEG here for offline review.

    Returns:
        VisionEoAResultEvent with proceed_with_pick=True always.
    """
    frame_path = ""
    if save_dir is not None:
        try:
            import datetime
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
            name = f"eoa_{ts}.jpg"
            out_path = save_dir / name
            cv2.imwrite(str(out_path), frame)
            frame_path = str(out_path)
            log.info("eoa.frame_saved", path=name)
        except Exception as exc:
            log.warning("eoa.save_failed", error=str(exc))

    return VisionEoAResultEvent(
        proceed_with_pick=True,
        refined_x=None,
        refined_y=None,
        frame_path=frame_path,
    )
