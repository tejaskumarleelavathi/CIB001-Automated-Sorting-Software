#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
method_b_adaptive_clahe.py — Bug Detection: CLAHE + Adaptive Threshold Method
==============================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Method B: Robust pipeline using Contrast Limited Adaptive Histogram
Equalisation (CLAHE) followed by adaptive Gaussian thresholding.

Advantages over Method A (global threshold):
    - CLAHE corrects uneven backlight illumination across the dish area.
    - Adaptive threshold uses a local neighbourhood mean rather than a fixed
      global cutoff, so detection is stable even when overall brightness shifts.
    - Morphological CLOSE after OPEN fills holes inside large bug bodies
      (e.g. transparent wing regions).

Pipeline stages:
    1. Grayscale conversion
    2. Locate backlit white panel (brightness threshold + largest contour)
    3. Detect Petri dish circle (Hough Circle Transform)
    4. Create interior dish mask (trims rim)
    5. CLAHE contrast enhancement
    6. Adaptive Gaussian inverse threshold
    7. Morphological open  (noise removal) + close (fill holes)
    8. Contour detection + area filter
    9. Annotate and save result image

Tuned for full-resolution images (~5320 × 4600 px) captured by the
Lucid TRI245S-CC overhead camera with DCS-100E backlight at 250 mA.

Usage (standalone):
    python method_b_adaptive_clahe.py                       # all images in default dir
    python method_b_adaptive_clahe.py path/to/image.jpg    # single image
    python method_b_adaptive_clahe.py --no-show --save ./out  # batch, headless
"""

from __future__ import annotations

import argparse
import logging
import sys
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
log = logging.getLogger("cib.method_b")

# ---------------------------------------------------------------------------
# Tunables — calibrated for 5320 × 4600 px, 250 mA backlight
# ---------------------------------------------------------------------------
# Adaptive threshold
ADAPTIVE_BLOCK_SIZE: int = 151   # Neighbourhood size (must be odd); large for hi-res
ADAPTIVE_C: int = 10             # Constant subtracted from local mean; raise to reject faint objects

# Bug size filter (px²)
MIN_BUG_AREA: int = 300          # Below this → dust speck / noise
MAX_BUG_AREA: int = 25_000       # Above this → merged cluster or large artefact

# Morphological cleanup
MORPH_KERNEL_SIZE: int = 5       # Elliptical kernel diameter in px
MORPH_OPEN_ITER: int = 1         # Opening iterations (noise removal)
MORPH_CLOSE_ITER: int = 2        # Closing iterations (fill holes in bug bodies)

# Petri dish mask
DISH_MARGIN: float = 0.96        # Use 96% of radius — trims the dish rim

# CLAHE illumination equalisation
CLAHE_CLIP: float = 3.0          # Clip limit (contrast cap per tile)
CLAHE_TILE: tuple[int, int] = (8, 8)  # Grid size for local histogram

# Backlight panel detection
BRIGHT_PANEL_THRESH: int = 70    # Minimum pixel value considered backlit

# Display
DISPLAY_HEIGHT: int = 900        # Resize window to this height for comfortable viewing


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class DetectionResult:
    """Holds the outcome of running Method B on one image."""

    image_path: str
    bug_count: int
    dish_cx: int
    dish_cy: int
    dish_radius: int
    centroids_px: list[tuple[float, float]] = field(default_factory=list)
    annotated_image: Optional[np.ndarray] = field(default=None, repr=False)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    """Convert a BGR image to grayscale.

    Args:
        img: BGR numpy array.

    Returns:
        Single-channel grayscale array.
    """
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _find_backlight_rect(
    gray: np.ndarray,
) -> Optional[tuple[int, int, int, int]]:
    """Locate the bright rectangular backlit panel in the image.

    Thresholds at BRIGHT_PANEL_THRESH and returns the bounding rect of the
    largest bright contour found after morphological closing.

    Args:
        gray: Grayscale image.

    Returns:
        (x, y, w, h) bounding rect of the backlit panel, or None if not found.
    """
    _, bright_mask = cv2.threshold(gray, BRIGHT_PANEL_THRESH, 255, cv2.THRESH_BINARY)

    # Close gap caused by the dish rim sitting on the panel
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 30))
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        log.warning("No backlit panel found — will search full frame for dish.")
        return None

    return cv2.boundingRect(max(contours, key=cv2.contourArea))


def _find_petri_dish(
    gray: np.ndarray,
    rect: Optional[tuple[int, int, int, int]],
) -> tuple[int, int, int]:
    """Detect the circular Petri dish using the Hough Circle Transform.

    Searches inside the bright panel ROI when available, otherwise the full
    frame. Falls back to the centre of the bright rect if no circle is found.

    Args:
        gray: Grayscale image.
        rect: Bounding rect of the backlit panel (or None for full frame).

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
        log.debug("Hough circle: centre=(%d,%d) radius=%dpx", cx, cy, c[2])
        return cx, cy, int(c[2])

    # ── Fallback ──────────────────────────────────────────────────────────────
    log.warning("Hough circle detection failed — using fallback centre.")
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

    Uses DISH_MARGIN to exclude the opaque plastic rim.

    Args:
        shape: Image shape (H, W) or (H, W, C).
        cx: Dish centre x.
        cy: Dish centre y.
        radius: Full dish radius.

    Returns:
        uint8 binary mask (255 inside, 0 outside).
    """
    mask = np.zeros(shape[:2], dtype=np.uint8)
    inner_r = int(radius * DISH_MARGIN)
    cv2.circle(mask, (cx, cy), inner_r, 255, -1)
    return mask


def _clahe_enhance(gray: np.ndarray) -> np.ndarray:
    """Apply CLAHE to correct uneven backlight illumination.

    CLAHE (Contrast Limited Adaptive Histogram Equalisation) boosts local
    contrast without amplifying noise globally, making dark bugs more
    distinguishable against a brighter or uneven background.

    Args:
        gray: Grayscale interior image (values outside dish should be 0).

    Returns:
        Contrast-enhanced grayscale image.
    """
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=CLAHE_TILE)
    return clahe.apply(gray)


def _adaptive_threshold_bugs(
    enhanced: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    """Apply adaptive Gaussian inverse threshold to isolate dark bugs.

    Uses a local neighbourhood mean (block size ADAPTIVE_BLOCK_SIZE) rather
    than a fixed global value, so detection adapts to lighting gradients
    across the dish.

    Args:
        enhanced: CLAHE-enhanced grayscale interior image.
        mask: Binary dish interior mask.

    Returns:
        Binary image with bug pixels = 255.
    """
    thresh = cv2.adaptiveThreshold(
        enhanced,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        ADAPTIVE_BLOCK_SIZE,
        ADAPTIVE_C,
    )
    # Clip anything outside the dish mask
    return cv2.bitwise_and(thresh, thresh, mask=mask)


def _morph_clean(binary: np.ndarray) -> np.ndarray:
    """Remove noise (opening) and fill holes in bug bodies (closing).

    Args:
        binary: Binary threshold image.

    Returns:
        Cleaned binary image.
    """
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE)
    )
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=MORPH_OPEN_ITER)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=MORPH_CLOSE_ITER)
    return closed


def _filter_contours(contours: tuple) -> list[np.ndarray]:
    """Filter contours by area to keep only plausible bug-sized blobs.

    Args:
        contours: Raw contours from cv2.findContours.

    Returns:
        Filtered list of contours within [MIN_BUG_AREA, MAX_BUG_AREA].
    """
    return [c for c in contours if MIN_BUG_AREA <= cv2.contourArea(c) <= MAX_BUG_AREA]


def _annotate(
    img: np.ndarray,
    bugs: list[np.ndarray],
    cx: int,
    cy: int,
    radius: int,
) -> np.ndarray:
    """Draw detection results on a copy of the original image.

    Draws:
        - Orange circle      : Petri dish boundary
        - Green rotated rect : Minimum area bounding box per bug (fits elongated insects)
        - Red dot            : Bug centroid
        - Blue label         : Bug index number
        - Top-left           : Bug count banner

    Args:
        img: Original BGR image.
        bugs: Filtered bug contours.
        cx: Dish centre x.
        cy: Dish centre y.
        radius: Dish radius.

    Returns:
        Annotated BGR image.
    """
    out = img.copy()

    # Dish boundary
    cv2.circle(out, (cx, cy), radius, (0, 165, 255), 6)

    for i, cnt in enumerate(bugs):
        # Rotated rect fits elongated insect bodies better than axis-aligned
        rect = cv2.minAreaRect(cnt)
        box = np.intp(cv2.boxPoints(rect))
        cv2.drawContours(out, [box], 0, (0, 255, 0), 2)

        M = cv2.moments(cnt)
        if M["m00"] > 0:
            bx = int(M["m10"] / M["m00"])
            by = int(M["m01"] / M["m00"])
            dot_r = max(4, int(img.shape[1] / 1000))
            txt_scale = max(0.4, img.shape[1] / 8000)
            cv2.circle(out, (bx, by), dot_r, (0, 0, 255), -1)
            cv2.putText(
                out, str(i + 1),
                (bx + dot_r + 2, by - dot_r),
                cv2.FONT_HERSHEY_SIMPLEX, txt_scale, (255, 80, 0), 1,
            )

    # Count banner
    label = f"[B] Bugs detected: {len(bugs)}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(1.0, img.shape[1] / 1500)
    thick = max(2, int(scale * 2))
    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    pad = int(scale * 12)
    cv2.rectangle(out, (pad, pad), (pad * 2 + tw, pad * 2 + th), (0, 0, 0), -1)
    cv2.putText(out, label, (int(pad * 1.5), pad + th), font, scale, (0, 255, 0), thick)

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_detection(image_path: Path | str) -> DetectionResult:
    """Run the full Method B pipeline on a single image.

    Args:
        image_path: Path to the input image (JPG or PNG).

    Returns:
        DetectionResult containing bug count, centroids, and annotated image.
        On failure, result.error is set and bug_count is 0.
    """
    image_path = Path(image_path)
    log.info("Method B | Processing: %s", image_path.name)

    try:
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(f"cv2.imread returned None for: {image_path}")

        gray = _to_gray(img)

        rect = _find_backlight_rect(gray)
        cx, cy, radius = _find_petri_dish(gray, rect)
        log.info("  Dish: centre=(%d,%d)  radius=%dpx", cx, cy, radius)

        mask = _make_interior_mask(gray.shape, cx, cy, radius)
        interior = cv2.bitwise_and(gray, gray, mask=mask)

        enhanced = _clahe_enhance(interior)
        thresh = _adaptive_threshold_bugs(enhanced, mask)
        cleaned = _morph_clean(thresh)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        bugs = _filter_contours(contours)
        log.info("  Bugs: %d detected", len(bugs))

        # Extract centroids in pixel coords
        centroids: list[tuple[float, float]] = []
        for cnt in bugs:
            M = cv2.moments(cnt)
            if M["m00"] > 0:
                centroids.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))

        annotated = _annotate(img, bugs, cx, cy, radius)

        return DetectionResult(
            image_path=str(image_path),
            bug_count=len(bugs),
            dish_cx=cx,
            dish_cy=cy,
            dish_radius=radius,
            centroids_px=centroids,
            annotated_image=annotated,
        )

    except FileNotFoundError as exc:
        log.error("Image not found: %s", exc)
        return DetectionResult(
            image_path=str(image_path),
            bug_count=0,
            dish_cx=0, dish_cy=0, dish_radius=0,
            error=str(exc),
        )
    except cv2.error as exc:
        log.error("OpenCV error processing %s: %s", image_path.name, exc)
        return DetectionResult(
            image_path=str(image_path),
            bug_count=0,
            dish_cx=0, dish_cy=0, dish_radius=0,
            error=f"OpenCV: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error processing %s", image_path.name)
        return DetectionResult(
            image_path=str(image_path),
            bug_count=0,
            dish_cx=0, dish_cy=0, dish_radius=0,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Method B — CLAHE + Adaptive Threshold Bug Detector (CIB001)"
    )
    parser.add_argument(
        "input", nargs="?",
        help="Image file or folder (default: CIB_Software/output/overhead_images)",
    )
    parser.add_argument("--save", "-s", default=None,
                        help="Directory to save annotated images.")
    parser.add_argument("--no-show", action="store_true",
                        help="Skip live display — useful for batch/headless mode.")
    return parser.parse_args()


def main() -> None:
    """CLI entry point: run Method B on one image or a whole folder."""
    args = _parse_args()

    default_dir = (
        Path(__file__).resolve().parents[2] / "output" / "overhead_images"
    )
    input_path = Path(args.input) if args.input else default_dir

    if input_path.is_file():
        paths = [input_path]
    elif input_path.is_dir():
        paths = sorted(
            list(input_path.glob("*.jpg"))
            + list(input_path.glob("*.jpeg"))
            + list(input_path.glob("*.png"))
        )
    else:
        log.error("'%s' is not a valid file or directory.", input_path)
        sys.exit(1)

    if not paths:
        log.error("No images found in: %s", input_path)
        sys.exit(1)

    save_dir = Path(args.save) if args.save else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  Method B — CLAHE + Adaptive  |  {len(paths)} image(s)")
    print(f"{'=' * 60}")

    total_bugs = 0
    for p in paths:
        result = run_detection(p)

        if result.error:
            print(f"  ERROR  {p.name}: {result.error}")
            continue

        total_bugs += result.bug_count

        if save_dir and result.annotated_image is not None:
            out_path = save_dir / f"B_{p.stem}_detected{p.suffix}"
            cv2.imwrite(str(out_path), result.annotated_image)
            log.info("  Saved: %s", out_path.name)

        if not args.no_show and result.annotated_image is not None:
            scale = DISPLAY_HEIGHT / result.annotated_image.shape[0]
            disp = cv2.resize(
                result.annotated_image,
                (int(result.annotated_image.shape[1] * scale), DISPLAY_HEIGHT),
            )
            cv2.imshow(f"Method B — {p.name}  (any key = next)", disp)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

    print(f"\n{'=' * 60}")
    print(f"  Images  : {len(paths)}")
    print(f"  Total bugs (Method B): {total_bugs}")
    print(f"  Average : {total_bugs / len(paths):.1f} per image")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
