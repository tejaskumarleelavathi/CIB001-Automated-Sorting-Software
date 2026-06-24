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

# Hair / filament thresholds
HAIR_ASPECT_THRESH: float = 4.5  # Filaments have very elongated bounding rects
HAIR_SOLIDITY_MAX: float = 0.40  # Filaments are thin → low area/hull ratio

# Bug thresholds
BUG_MIN_DIAM: float = 25.0       # px — equivalent circle diameter, rejects tiny specks
BUG_SOLIDITY_MIN: float = 0.18   # Bugs have appendages → some concavity in convex hull
BUG_SOLIDITY_MAX: float = 0.92   # Front-light-off images produce compact silhouettes (0.85+)

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


def _select_dominant(blobs: list[BlobFeatures], frame_shape: tuple) -> Optional[BlobFeatures]:
    """Choose the dominant specimen blob — largest blob above MIN_CONTOUR_AREA.

    In EoA images the arm presents one specimen; the largest dark blob is the specimen.
    If multiple blobs are within 50% area of the largest, pick the one closest to centre.

    Args:
        blobs: All detected blobs, sorted by area descending.
        frame_shape: (H, W[, C]) of the original frame.

    Returns:
        The dominant BlobFeatures, or None if no blobs found.
    """
    if not blobs:
        return None

    largest_area = blobs[0].area
    candidates = [b for b in blobs if b.area >= 0.50 * largest_area]

    if len(candidates) == 1:
        return candidates[0]

    # Break ties by proximity to frame centre
    cy_frame = frame_shape[0] / 2.0
    cx_frame = frame_shape[1] / 2.0
    candidates.sort(
        key=lambda b: (b.centroid_x - cx_frame) ** 2 + (b.centroid_y - cy_frame) ** 2
    )
    return candidates[0]


def _classify_blob(blob: BlobFeatures) -> tuple[str, str]:
    """Apply rule-based classification to a blob's shape features.

    Args:
        blob: Extracted features for the dominant specimen.

    Returns:
        (label, reason) where label is "BUG" | "HAIR" | "DEBRIS".
    """
    d = blob.equiv_diameter
    ar = blob.aspect_ratio
    sol = blob.solidity

    # Hair / filament: very elongated bounding rect AND low solidity
    if ar > HAIR_ASPECT_THRESH and sol < HAIR_SOLIDITY_MAX:
        return "HAIR", (
            f"aspect_ratio={ar:.1f}>{HAIR_ASPECT_THRESH} "
            f"and solidity={sol:.2f}<{HAIR_SOLIDITY_MAX}"
        )

    # Bug: sufficient size, solidity in the 'has-appendages' window
    if d >= BUG_MIN_DIAM and BUG_SOLIDITY_MIN <= sol <= BUG_SOLIDITY_MAX:
        return "BUG", (
            f"equiv_diam={d:.0f}>={BUG_MIN_DIAM:.0f} "
            f"and solidity={sol:.2f} in [{BUG_SOLIDITY_MIN},{BUG_SOLIDITY_MAX}]"
        )

    # Everything else — small fragment, smooth compact debris, or ambiguous
    return "DEBRIS", (
        f"equiv_diam={d:.0f} solidity={sol:.2f} aspect_ratio={ar:.1f} "
        f"(no rule matched)"
    )


def _annotate(
    img: np.ndarray,
    result: ClassificationResult,
    dominant: BlobFeatures,
) -> np.ndarray:
    """Draw classification result onto the image.

    Draws:
        - Orange convex hull of dominant blob
        - Green filled contour of dominant blob
        - Red centroid dot
        - Top-left banner: label + key features

    Args:
        img: Original BGR or grayscale image.
        result: Classification result.
        dominant: Features of the dominant blob.

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

    # Draw dominant contour
    if dominant.contour is not None:
        cv2.drawContours(out, [dominant.contour], -1, colour, 2)

    # Draw convex hull in orange
    if dominant.convex_hull is not None:
        cv2.polylines(out, [dominant.convex_hull], True, (0, 165, 255), 2)

    # Centroid dot
    cx_i = int(round(dominant.centroid_x))
    cy_i = int(round(dominant.centroid_y))
    cv2.circle(out, (cx_i, cy_i), 6, (0, 0, 255), -1)

    # Banner text
    lines = [
        f"Label: {result.label}",
        f"area={dominant.area:.0f}px2  diam={dominant.equiv_diameter:.0f}px",
        f"aspect={dominant.aspect_ratio:.2f}  solid={dominant.solidity:.2f}",
        f"circ={dominant.circularity:.2f}  extent={dominant.extent:.2f}",
    ]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.5, img.shape[1] / 1800)
    thick = max(1, int(scale * 2))
    line_h = int(cv2.getTextSize("A", font, scale, thick)[0][1] * 2.2)
    pad = 10

    panel_h = pad * 2 + line_h * len(lines)
    panel_w = int(img.shape[1] * 0.52)
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
            )

        closed = _morphological_close(binary)
        blobs = _find_blobs(closed)

        log.info("  Blobs found: %d (above %d px²)", len(blobs), MIN_CONTOUR_AREA)

        if not blobs:
            result = ClassificationResult(
                image_path=str(image_path),
                label="NONE",
                is_bug=False,
                dominant_blob=None,
                all_blobs=[],
                notes="No foreground blobs detected",
                otsu_threshold=otsu_thresh,
                fg_coverage=fg_coverage,
            )
            log.info("  -> NONE (no blobs)")
            return result

        dominant = _select_dominant(blobs, gray.shape)
        label, reason = _classify_blob(dominant)

        log.info(
            "  Dominant: area=%.0f diam=%.0f aspect=%.2f solid=%.2f -> %s",
            dominant.area, dominant.equiv_diameter,
            dominant.aspect_ratio, dominant.solidity, label,
        )
        log.info("  Reason: %s", reason)

        annotated = _annotate(img, ClassificationResult(
            image_path=str(image_path), label=label, is_bug=(label == "BUG"),
            dominant_blob=dominant,
        ), dominant)

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
        )
    except cv2.error as exc:
        log.error("OpenCV error on %s: %s", image_path.name, exc)
        return ClassificationResult(
            image_path=str(image_path), label="NONE", is_bug=False,
            dominant_blob=None, error=f"OpenCV: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error on %s", image_path.name)
        return ClassificationResult(
            image_path=str(image_path), label="NONE", is_bug=False,
            dominant_blob=None, error=str(exc),
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

    print(f"\n{'=' * 72}")
    print(f"  EoA Specimen Classifier  |  {len(paths)} image(s)")
    print(f"  Thresholds: Otsu auto-threshold  hair_ar>{HAIR_ASPECT_THRESH}"
          f"  hair_sol<{HAIR_SOLIDITY_MAX}  bug_diam>={BUG_MIN_DIAM:.0f}")
    print(f"{'=' * 72}")
    print(
        f"{'Image':<35} {'Label':<8} {'Otsu':>5} {'FGcov':>6} {'Area':>8} "
        f"{'Diam':>6} {'AspRt':>6} {'Solid':>6} {'Circ':>6}"
    )
    print("-" * 85)

    results: list[ClassificationResult] = []
    for p in paths:
        r = classify_eoa_image(p, save_dir=save_dir if not args.features_only else None)
        results.append(r)

        otsu_str = f"{r.otsu_threshold:>5.0f}"
        cov_str  = f"{r.fg_coverage * 100:>5.1f}%"

        if r.error:
            print(f"{p.name:<35} {'ERROR':<8} {otsu_str} {cov_str}  {r.error}")
            continue

        d = r.dominant_blob
        if d is None:
            print(f"{p.name:<35} {'NONE':<8} {otsu_str} {cov_str}")
            continue

        warn = " !" if r.fg_coverage > MAX_FG_COVERAGE else ""
        print(
            f"{p.name:<35} {r.label:<8} {otsu_str} {cov_str} "
            f"{d.area:>8.0f} {d.equiv_diameter:>6.1f} "
            f"{d.aspect_ratio:>6.2f} {d.solidity:>6.2f} {d.circularity:>6.2f}{warn}"
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

    print(f"\n{'=' * 72}")
    print(f"  Images : {len(paths)}")
    print(f"  BUG    : {bugs}")
    print(f"  HAIR   : {hairs}")
    print(f"  DEBRIS : {debris}")
    print(f"  NONE   : {none_c}")
    print(f"  ERRORS : {errors}")
    if not args.features_only:
        print(f"  Saved  : {save_dir}")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
