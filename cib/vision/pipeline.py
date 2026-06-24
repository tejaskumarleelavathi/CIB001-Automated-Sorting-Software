"""
pipeline.py — Overhead camera vision pipeline for bug detection.
================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Detects insect specimens in backlit Petri dish images captured by the
Lucid TRI245S-CC overhead camera with DCS-100E backlight at 250 mA.

Detection algorithm (validated as Method C in software_tests/overhead_detection/):
    1. Grayscale conversion
    2. Locate backlit white panel — brightness threshold + largest contour
    3. Detect Petri dish circle — Hough Circle Transform within panel ROI
    4. Create interior dish mask — trims opaque rim (DISH_MARGIN fraction)
    5. Global inverse threshold — pixels below THRESHOLD_VALUE are bugs
    6. Morphological open — removes isolated single-pixel noise
    7. Contour detection — NO minimum area filter, keeps tiny specimens
    8. Pixel → robot XY transform via calibration homography
    9. Annotate and save result image

The algorithm was chosen over adaptive-threshold alternatives (Methods A/B)
because it detects very small specimens that would otherwise be rejected by
a fixed area filter, while remaining fast enough for real-time operation.

Public interface:
    run_overhead_detection(frame, save_dir, save_format) -> VisionOverheadResultEvent
"""

from __future__ import annotations

import structlog
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cib.core.events import VisionOverheadResultEvent

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunables — calibrated for 5320 × 4600 px, DCS-100E backlight at 250 mA
# ---------------------------------------------------------------------------

# Bug detection
THRESHOLD_VALUE: int = 120    # Grayscale value below which a pixel is a bug (0–255)
MAX_BUG_AREA: int = 25_000    # Upper contour area limit in px² — rejects merged clusters
MIN_BUG_AREA: int = 1         # No lower limit — retains tiny specimens

# Morphological cleanup
MORPH_KERNEL_SIZE: int = 5    # Elliptical kernel diameter for morphological open (px)
MORPH_OPEN_ITER: int = 1      # Opening iterations — removes isolated single-pixel noise

# Petri dish detection
DISH_MARGIN: float = 0.91     # Interior mask uses this fraction of dish radius
DISH_RADIUS_EXPAND: float = 1.03  # Expand Hough-detected radius to true dish edge
BRIGHT_PANEL_THRESH: int = 70     # Min pixel value considered part of backlit panel


# ---------------------------------------------------------------------------
# Stage helpers — all private, called only by run_overhead_detection()
# ---------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    """Convert a BGR image to grayscale; pass through if already single-channel.

    Args:
        img: BGR or grayscale numpy array from camera driver.

    Returns:
        Single-channel uint8 grayscale array.
    """
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _find_backlight_rect(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Locate the bright rectangular backlit panel in the image.

    Applies a brightness threshold then finds the largest bright contour.
    A 30×30 morphological close fills the gap that the dish rim creates.

    Args:
        gray: Full-frame grayscale image.

    Returns:
        (x, y, w, h) bounding rect of the backlit panel, or None if not found.
    """
    _, bright_mask = cv2.threshold(gray, BRIGHT_PANEL_THRESH, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 30))
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        log.warning("overhead_pipeline.no_backlight_panel_found")
        return None

    return cv2.boundingRect(max(contours, key=cv2.contourArea))


def _find_petri_dish(
    gray: np.ndarray,
    rect: tuple[int, int, int, int] | None,
) -> tuple[int, int, int]:
    """Detect the circular Petri dish using the Hough Circle Transform.

    Searches inside the bright panel ROI when available (faster, more accurate),
    otherwise falls back to the full frame. If no circle is found, returns the
    centre of the bright rect or image centre as a fallback.

    Args:
        gray: Full-frame grayscale image.
        rect: Bounding rect (x, y, w, h) of the backlit panel, or None.

    Returns:
        (cx, cy, radius) in full-image pixel coordinates.
    """
    h, w = gray.shape

    if rect is not None:
        rx, ry, rw, rh = rect
        roi = gray[ry : ry + rh, rx : rx + rw]
        min_r = int(min(rw, rh) * 0.35)
        max_r = int(min(rw, rh) * 0.52)
        off_x, off_y = rx, ry
    else:
        roi = gray
        min_r = int(min(h, w) * 0.25)
        max_r = int(min(h, w) * 0.50)
        off_x, off_y = 0, 0

    blurred = cv2.GaussianBlur(roi, (13, 13), 3)
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(roi.shape),   # Expect exactly one dish per frame
        param1=60,
        param2=28,
        minRadius=min_r,
        maxRadius=max_r,
    )

    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        cx = int(c[0] + off_x)
        cy = int(c[1] + off_y)
        radius = int(c[2] * DISH_RADIUS_EXPAND)
        log.debug("overhead_pipeline.dish_detected", cx=cx, cy=cy, radius=radius)
        return cx, cy, radius

    # ── Fallback: centre of bright rect or image centre ───────────────────────
    log.warning("overhead_pipeline.hough_failed_using_fallback")
    if rect is not None:
        rx, ry, rw, rh = rect
        return rx + rw // 2, ry + rh // 2, int(min(rw, rh) * 0.44)
    return w // 2, h // 2, int(min(h, w) * 0.40)


def _make_interior_mask(
    shape: tuple[int, ...],
    cx: int,
    cy: int,
    radius: int,
) -> np.ndarray:
    """Build a circular binary mask covering the dish interior.

    Uses DISH_MARGIN so the opaque plastic rim is excluded from analysis.

    Args:
        shape: Image shape (H, W) or (H, W, C).
        cx: Dish centre x in px.
        cy: Dish centre y in px.
        radius: Full dish radius in px.

    Returns:
        uint8 binary mask — 255 inside the interior, 0 outside.
    """
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.circle(mask, (cx, cy), int(radius * DISH_MARGIN), 255, -1)
    return mask


def _threshold_bugs(gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Isolate dark bug pixels via global inverse threshold.

    Pixels below THRESHOLD_VALUE (dark bugs on bright background) become white.
    The result is clipped to the dish interior mask to exclude the rim.

    Args:
        gray: Masked grayscale interior image.
        mask: Binary dish interior mask.

    Returns:
        Binary image with bug pixels = 255.
    """
    _, bugs_white = cv2.threshold(gray, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY_INV)
    return cv2.bitwise_and(bugs_white, bugs_white, mask=mask)


def _morph_clean(binary: np.ndarray) -> np.ndarray:
    """Remove isolated single-pixel noise via morphological opening.

    The kernel is intentionally small (MORPH_KERNEL_SIZE) so that
    tiny bug blobs are not erased during the opening operation.

    Args:
        binary: Binary threshold image.

    Returns:
        Cleaned binary image.
    """
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE)
    )
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=MORPH_OPEN_ITER)


def _extract_centroids(
    binary: np.ndarray,
) -> tuple[list[tuple[float, float]], list[np.ndarray]]:
    """Find bug contours and compute their centroids in pixel coordinates.

    Applies only an upper area bound (MAX_BUG_AREA) to reject merged clusters.
    No lower bound is applied so that very small specimens are retained.

    Args:
        binary: Cleaned binary image from _morph_clean().

    Returns:
        centroids_px: List of (x, y) float tuples in full-image pixel coords.
        contours:     Corresponding list of cv2 contours (for annotation).
    """
    raw_contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    centroids_px: list[tuple[float, float]] = []
    contours: list[np.ndarray] = []

    for cnt in raw_contours:
        area = cv2.contourArea(cnt)
        if not (MIN_BUG_AREA <= area <= MAX_BUG_AREA):
            continue
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            centroids_px.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))
            contours.append(cnt)

    return centroids_px, contours


def _annotate_frame(
    frame: np.ndarray,
    contours: list[np.ndarray],
    cx: int,
    cy: int,
    radius: int,
    count: int,
    timestamp: datetime,
) -> np.ndarray:
    """Draw detection annotations on a copy of the original frame.

    Annotations:
        - Orange circle  : Petri dish boundary
        - Green circle   : Minimum enclosing circle per bug
        - Red dot        : Bug centroid
        - Top-left       : Specimen count + timestamp banner

    Args:
        frame:     Original BGR (or grayscale) image array.
        contours:  Bug contours returned by _extract_centroids().
        cx:        Dish centre x.
        cy:        Dish centre y.
        radius:    Dish radius.
        count:     Total specimen count (for banner label).
        timestamp: Capture timestamp for the banner.

    Returns:
        Annotated BGR image as a numpy array.
    """
    out = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

    # Dish boundary
    cv2.circle(out, (cx, cy), radius, (0, 165, 255), 6)

    # Bug detections
    for cnt in contours:
        (bx, by), r = cv2.minEnclosingCircle(cnt)
        cv2.circle(out, (int(bx), int(by)), max(1, int(r)), (0, 255, 0), 2)
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            cx_b = int(M["m10"] / M["m00"])
            cy_b = int(M["m01"] / M["m00"])
            cv2.circle(out, (cx_b, cy_b), 5, (0, 0, 255), -1)

    # Count + timestamp banner
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(1.0, frame.shape[1] / 1500)
    thick = max(2, int(scale * 2))
    label = f"Detected: {count} specimen(s)"
    ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    pad = int(scale * 12)
    cv2.rectangle(out, (pad, pad), (pad * 2 + tw, pad * 2 + th), (0, 0, 0), -1)
    cv2.putText(out, label, (int(pad * 1.5), pad + th), font, scale, (0, 255, 0), thick)
    cv2.putText(out, ts_str, (pad, pad * 2 + th + int(scale * 30)),
                font, scale * 0.6, (200, 200, 200), 1)

    return out


def _save_annotated_image(
    annotated: np.ndarray,
    save_dir: Path,
    save_format: str,
    timestamp: datetime,
) -> str:
    """Save an annotated image to disk.

    Filename format: overhead_{YYYYMMDD}_{HHMMSS}_{ms}.{format}

    Args:
        annotated:   BGR annotated image array.
        save_dir:    Target directory (created if it does not exist).
        save_format: "png" or "tiff".
        timestamp:   Capture timestamp used to build the filename.

    Returns:
        Absolute path string of the saved file, or "" on failure.
    """
    try:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        filename = (
            f"overhead_{timestamp.strftime('%Y%m%d')}_{timestamp.strftime('%H%M%S')}"
            f"_{timestamp.microsecond // 1000:03d}.{save_format}"
        )
        out_path = save_dir / filename

        if save_format.lower() == "tiff":
            cv2.imwrite(str(out_path), annotated)
        else:
            cv2.imwrite(str(out_path), annotated, [cv2.IMWRITE_PNG_COMPRESSION, 3])

        log.info("overhead_pipeline.image_saved", path=str(out_path))
        return str(out_path)

    except OSError as exc:
        log.error("overhead_pipeline.save_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_overhead_detection(
    frame: Any,
    save_dir: Path | None = None,
    save_format: str = "png",
) -> VisionOverheadResultEvent:
    """Run the full overhead bug detection pipeline on one camera frame.

    Detects all insect specimens inside the Petri dish, transforms their
    pixel centroids to robot XY coordinates using the loaded calibration
    homography, and optionally saves an annotated image to disk.

    If the calibration file is not found (e.g., during development without
    hardware), pixel coordinates are returned as-is with a warning logged.

    Args:
        frame:       Grayscale or BGR numpy array from GigECameraDriver or
                     MockCameraDriver.
        save_dir:    Directory for saving the annotated image. Pass None to
                     skip saving.
        save_format: Image format — "png" (default) or "tiff".

    Returns:
        VisionOverheadResultEvent with:
            centroids_robot_xy  — specimen positions in robot coordinate space
            annotated_frame_path — path to saved annotated image, or ""
            sample_count        — total number of specimens detected
    """
    timestamp = datetime.now()

    try:
        gray = _to_gray(frame)

        # ── Stage 1: Locate dish ───────────────────────────────────────────────
        rect = _find_backlight_rect(gray)
        cx, cy, radius = _find_petri_dish(gray, rect)
        log.info(
            "overhead_pipeline.dish_located",
            cx=cx, cy=cy, radius=radius,
        )

        # ── Stage 2: Threshold + cleanup ──────────────────────────────────────
        mask = _make_interior_mask(gray.shape, cx, cy, radius)
        interior = cv2.bitwise_and(gray, gray, mask=mask)
        thresh = _threshold_bugs(interior, mask)
        cleaned = _morph_clean(thresh)

        # ── Stage 3: Extract centroids ────────────────────────────────────────
        centroids_px, contours = _extract_centroids(cleaned)
        log.info(
            "overhead_pipeline.detection_complete",
            specimen_count=len(centroids_px),
        )

        # ── Stage 4: Pixel → robot XY ─────────────────────────────────────────
        centroids_robot_xy: list[tuple[float, float]]
        try:
            from cib.vision.calibration import load_calibration
            calib = load_calibration()
            centroids_robot_xy = calib.pixel_list_to_robot(centroids_px)
        except Exception as calib_exc:  # noqa: BLE001
            log.warning(
                "overhead_pipeline.calibration_unavailable",
                error=str(calib_exc),
                fallback="returning raw pixel coordinates",
            )
            centroids_robot_xy = centroids_px

        # ── Stage 5: Annotate + save ──────────────────────────────────────────
        saved_path = ""
        if save_dir is not None:
            annotated = _annotate_frame(
                frame, contours, cx, cy, radius, len(centroids_px), timestamp
            )
            saved_path = _save_annotated_image(
                annotated, Path(save_dir), save_format, timestamp
            )

        return VisionOverheadResultEvent(
            centroids_robot_xy=centroids_robot_xy,
            annotated_frame_path=saved_path,
            sample_count=len(centroids_robot_xy),
        )

    except cv2.error as exc:
        log.error("overhead_pipeline.opencv_error", error=str(exc))
        return VisionOverheadResultEvent(
            centroids_robot_xy=[],
            annotated_frame_path="",
            sample_count=0,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("overhead_pipeline.unexpected_error", error=str(exc))
        return VisionOverheadResultEvent(
            centroids_robot_xy=[],
            annotated_frame_path="",
            sample_count=0,
        )
