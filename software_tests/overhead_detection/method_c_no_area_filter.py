#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
method_c_no_area_filter.py — Bug Detection: No Area Filter Method
=================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Method C: Same global-threshold pipeline as Method A but with NO minimum
area filter on contours. This allows detection of very small/tiny bugs that
would be rejected by the 300 px² floor in Methods A and B.

Key difference from Method A:
    - Method A/B reject contours < MIN_BUG_AREA (300 px²)
    - Method C keeps ALL contours above 1 px² (effectively no lower limit)
    - Only the upper bound (MAX_BUG_AREA) is applied to reject merged clusters

This mirrors the logic in hardware_tests/test_bug_detection.py which was
observed to detect tiny bugs that Methods A and B miss.

Pipeline stages:
    1. Grayscale conversion
    2. Locate backlit white panel (brightness threshold + largest contour)
    3. Detect Petri dish circle (Hough Circle Transform)
    4. Create interior dish mask (trims rim)
    5. Global inverse threshold  — pixels below THRESHOLD_VALUE = bug
    6. Morphological open        — remove isolated single-pixel noise
    7. Contour detection — NO minimum area filter
    8. Annotate and save result image

Usage (standalone):
    python method_c_no_area_filter.py                       # all images in default dir
    python method_c_no_area_filter.py path/to/image.jpg    # single image
    python method_c_no_area_filter.py --no-show --save ./out  # batch, headless
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
log = logging.getLogger("cib.method_c")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
THRESHOLD_VALUE: int = 120       # Pixels below this are classified as bugs (0–255)
MIN_BUG_AREA: int = 1            # Effectively no lower bound — keeps tiny bugs
MAX_BUG_AREA: int = 25_000       # Upper bound only — rejects merged clusters
MORPH_KERNEL_SIZE: int = 5       # Morphological kernel diameter in px
MORPH_OPEN_ITER: int = 1         # Opening removes only isolated single-pixel noise
DISH_MARGIN: float = 0.91        # Fraction of dish radius used as interior mask
DISH_RADIUS_EXPAND: float = 1.03 # Expand detected Hough radius to true dish edge
BRIGHT_PANEL_THRESH: int = 70    # Minimum pixel value considered part of backlit panel
DISPLAY_HEIGHT: int = 900        # Window height when showing results


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class DetectionResult:
    """Holds the outcome of running Method C on one image."""

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
    """Convert a BGR image to grayscale. Pass through if already single-channel.

    Args:
        img: BGR or grayscale numpy array.

    Returns:
        Single-channel grayscale array.
    """
    if len(img.shape) == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _find_backlight_rect(
    gray: np.ndarray,
) -> Optional[tuple[int, int, int, int]]:
    """Locate the bright rectangular backlit panel in the image.

    Args:
        gray: Grayscale image.

    Returns:
        (x, y, w, h) bounding rect of the backlit panel, or None if not found.
    """
    _, bright_mask = cv2.threshold(gray, BRIGHT_PANEL_THRESH, 255, cv2.THRESH_BINARY)
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
        minDist=max(roi.shape),
        param1=60,
        param2=28,
        minRadius=min_r,
        maxRadius=max_r,
    )

    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        expanded_r = int(c[2] * DISH_RADIUS_EXPAND)
        return int(c[0] + off_x), int(c[1] + off_y), expanded_r

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

    Args:
        shape: Image shape (H, W) or (H, W, C).
        cx: Dish centre x.
        cy: Dish centre y.
        radius: Full dish radius.

    Returns:
        uint8 binary mask (255 inside, 0 outside).
    """
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.circle(mask, (cx, cy), int(radius * DISH_MARGIN), 255, -1)
    return mask


def _threshold_bugs(gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Apply global inverse threshold to isolate dark bugs.

    Args:
        gray: Masked grayscale interior image.
        mask: Binary dish interior mask.

    Returns:
        Binary image with bug pixels = 255.
    """
    _, bugs_white = cv2.threshold(gray, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY_INV)
    return cv2.bitwise_and(bugs_white, bugs_white, mask=mask)


def _morph_clean(binary: np.ndarray) -> np.ndarray:
    """Remove single-pixel noise via morphological opening.

    Kernel is kept small so that tiny bug blobs are preserved.

    Args:
        binary: Binary threshold image.

    Returns:
        Cleaned binary image.
    """
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE)
    )
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=MORPH_OPEN_ITER)


def _filter_contours(contours: tuple) -> list[np.ndarray]:
    """Keep all contours up to MAX_BUG_AREA. No lower bound applied.

    This is the key difference from Methods A and B: tiny blobs (< 300 px²)
    are retained here so that very small specimens are not discarded.

    Args:
        contours: Raw contours from cv2.findContours.

    Returns:
        All contours with area <= MAX_BUG_AREA.
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
        - Orange circle : Petri dish boundary
        - Green circle  : Minimum enclosing circle per detected blob
        - Red dot       : Bug centroid
        - Top-left      : Bug count banner labelled [C]

    Args:
        img: Original BGR (or grayscale) image.
        bugs: Filtered bug contours.
        cx: Dish centre x.
        cy: Dish centre y.
        radius: Dish radius.

    Returns:
        Annotated BGR image.
    """
    out = img.copy() if len(img.shape) == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    cv2.circle(out, (cx, cy), radius, (0, 165, 255), 6)

    for cnt in bugs:
        (bx, by), r = cv2.minEnclosingCircle(cnt)
        cv2.circle(out, (int(bx), int(by)), max(1, int(r)), (0, 255, 0), 2)
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            cx_b = int(M["m10"] / M["m00"])
            cy_b = int(M["m01"] / M["m00"])
            cv2.circle(out, (cx_b, cy_b), 5, (0, 0, 255), -1)

    label = f"[C] Bugs detected: {len(bugs)}"
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
    """Run the full Method C pipeline on a single image.

    Args:
        image_path: Path to the input image (JPG or PNG).

    Returns:
        DetectionResult containing bug count, centroids, and annotated image.
        On failure, result.error is set and bug_count is 0.
    """
    image_path = Path(image_path)
    log.info("Method C | Processing: %s", image_path.name)

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

        thresh = _threshold_bugs(interior, mask)
        cleaned = _morph_clean(thresh)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        bugs = _filter_contours(contours)
        log.info("  Bugs: %d detected (no min-area filter)", len(bugs))

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
        description="Method C — No Area Filter Bug Detector (CIB001)"
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
    """CLI entry point: run Method C on one image or a whole folder."""
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
    print(f"  Method C — No Area Filter  |  {len(paths)} image(s)")
    print(f"{'=' * 60}")

    total_bugs = 0
    for p in paths:
        result = run_detection(p)

        if result.error:
            print(f"  ERROR  {p.name}: {result.error}")
            continue

        total_bugs += result.bug_count

        if save_dir and result.annotated_image is not None:
            out_path = save_dir / f"C_{p.stem}_detected{p.suffix}"
            cv2.imwrite(str(out_path), result.annotated_image)
            log.info("  Saved: %s", out_path.name)

        if not args.no_show and result.annotated_image is not None:
            scale = DISPLAY_HEIGHT / result.annotated_image.shape[0]
            disp = cv2.resize(
                result.annotated_image,
                (int(result.annotated_image.shape[1] * scale), DISPLAY_HEIGHT),
            )
            cv2.imshow(f"Method C — {p.name}  (any key = next)", disp)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

    print(f"\n{'=' * 60}")
    print(f"  Images  : {len(paths)}")
    print(f"  Total bugs (Method C): {total_bugs}")
    print(f"  Average : {total_bugs / len(paths):.1f} per image")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
