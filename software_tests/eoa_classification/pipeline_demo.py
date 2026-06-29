#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_demo.py — End-to-End Vision Pipeline Test
===================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Simulates the full pick-and-place vision pipeline offline using saved images:

    Stage 1 — Overhead detection
        Run method C on an overhead image
        -> list of bug pixel centroids within the dish

    Stage 2 — Bug selection
        Sort bugs by distance from the dish top-left (corner-first traversal)
        Pick the highest-priority unprocessed bug

    Stage 3 — EoA classification
        Run the EoA classifier on the arm-camera image
        -> BUG | NONE

    Stage 4 — Body center (pipette target)
        If BUG: compute intensity-weighted centroid of dark body pixels only
        (excludes semi-transparent wings so pipette hits thorax, not wing)
        -> (cx, cy) in pixel space, ready for homography -> robot XY

In production:
    - Stage 1 uses the overhead LUCID camera
    - Stage 2 result is sent to the robot controller as a move target
    - After the robot moves, Stage 3 uses the EoA LUCID camera
    - Stage 4 result is sent to the robot as the pipette drop point

Usage:
    python pipeline_demo.py --overhead <overhead_image> --eoa <eoa_image>
    python pipeline_demo.py --overhead img.jpg --eoa arm.jpg --save ./out
    python pipeline_demo.py --overhead img.jpg --eoa arm.jpg --no-show

Arguments:
    --overhead   Path to an overhead camera image (required)
    --eoa        Path to an EoA arm camera image (required)
    --save       Directory to write annotated outputs (default: ./results/)
    --no-show    Skip live display windows (headless / batch mode)
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Local imports — both modules live in the same directory
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from eoa_classifier import classify_eoa_image, ClassificationResult  # noqa: E402

# Overhead detection: use method C (no minimum area filter)
_OVERHEAD_MODULE = _HERE / "method_c_no_area_filter.py"
if not _OVERHEAD_MODULE.exists():
    # Fall back to looking two levels up in software_tests
    _OVERHEAD_MODULE = _HERE.parent / "overhead_detection" / "method_c_no_area_filter.py"
sys.path.insert(0, str(_OVERHEAD_MODULE.parent))
from method_c_no_area_filter import run_detection as run_overhead_detection  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cib.pipeline_demo")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISPLAY_HEIGHT: int = 900       # px — window height for display


# ---------------------------------------------------------------------------
# Stage 2: Bug selection
# ---------------------------------------------------------------------------

def select_bug(
    centroids_px: list[tuple[float, float]],
    dish_cx: int,
    dish_cy: int,
    dish_radius: int,
) -> tuple[tuple[float, float], list[tuple[float, float]]]:
    """Select the next bug to pick using corner-first traversal.

    Sorts all detected bug centroids by their distance from the top-left of
    the dish interior, then returns the closest one as the pick target.

    Corner-first ordering ensures the robot works systematically across the
    dish without needing to revisit regions.

    Args:
        centroids_px:  List of (cx, cy) bug centroids in pixel space.
        dish_cx:       Dish centre x in pixels.
        dish_cy:       Dish centre y in pixels.
        dish_radius:   Dish radius in pixels.

    Returns:
        (selected, remaining) — the top-priority centroid and the rest in order.

    Raises:
        ValueError: if centroids_px is empty.
    """
    if not centroids_px:
        raise ValueError("No bug centroids to select from")

    # Reference point: top-left of dish interior
    ref_x = dish_cx - dish_radius * 0.707   # 45-degree corner
    ref_y = dish_cy - dish_radius * 0.707

    def dist_to_ref(c: tuple[float, float]) -> float:
        return math.sqrt((c[0] - ref_x) ** 2 + (c[1] - ref_y) ** 2)

    ranked = sorted(centroids_px, key=dist_to_ref)
    return ranked[0], ranked[1:]


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def annotate_overhead(
    img_path: Path,
    centroids: list[tuple[float, float]],
    selected: tuple[float, float],
    dish_cx: int,
    dish_cy: int,
    dish_radius: int,
) -> np.ndarray:
    """Draw bug detections on the overhead image.

    Draws:
        - Orange circle: dish boundary
        - Green dot: each detected bug centroid
        - Cyan crosshair: selected bug (pick target for this cycle)
        - Top-left banner: bug count + selected centroid

    Args:
        img_path:    Path to original overhead image.
        centroids:   All detected bug centroids in pixel space.
        selected:    The chosen pick target centroid.
        dish_cx/cy:  Dish centre.
        dish_radius: Dish radius.

    Returns:
        Annotated BGR image.
    """
    img = cv2.imread(str(img_path))
    if img is None:
        raise FileNotFoundError(f"Cannot read overhead image: {img_path}")

    # Dish boundary
    cv2.circle(img, (dish_cx, dish_cy), dish_radius, (0, 165, 255), 4)

    # All centroids
    for cx, cy in centroids:
        cv2.circle(img, (int(cx), int(cy)), 8, (0, 200, 0), -1)

    # Selected bug: crosshair
    sx, sy = int(selected[0]), int(selected[1])
    arm = 30
    cv2.line(img, (sx - arm, sy), (sx + arm, sy), (255, 255, 0), 3)
    cv2.line(img, (sx, sy - arm), (sx, sy + arm), (255, 255, 0), 3)
    cv2.circle(img, (sx, sy), arm // 2, (255, 255, 0), 3)

    # Banner
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.6, img.shape[1] / 1500)
    thick = max(2, int(scale * 2))
    lines = [
        f"Stage 1: Overhead  |  {len(centroids)} bug(s) detected",
        f"Stage 2: Selected  ({selected[0]:.0f}, {selected[1]:.0f}) px  [corner-first]",
    ]
    line_h = int(cv2.getTextSize("A", font, scale, thick)[0][1] * 2.5)
    pad = 12
    panel_h = pad * 2 + line_h * len(lines)
    panel_w = int(img.shape[1] * 0.7)
    cv2.rectangle(img, (0, 0), (panel_w, panel_h), (20, 20, 20), -1)
    for i, text in enumerate(lines):
        cv2.putText(
            img, text, (pad, pad + (i + 1) * line_h),
            font, scale, (220, 220, 220), thick,
        )

    return img


def annotate_eoa_stage(result: ClassificationResult, stage: int = 3) -> np.ndarray:
    """Return the already-annotated EoA image with stage label added.

    The EoA classifier's _annotate() already draws the blob, body center
    crosshair, and banner.  This function just prepends the stage number
    so the demo output is clearly labelled.

    Args:
        result: ClassificationResult from classify_eoa_image().
        stage:  Stage number to show in the banner (3 = classify, 4 = body center).

    Returns:
        Annotated BGR image (copy of result.annotated_image with stage tag).
    """
    if result.annotated_image is None:
        raise ValueError("No annotated image in ClassificationResult")

    out = result.annotated_image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.5, out.shape[1] / 1800)
    thick = max(1, int(scale * 2))

    stage_labels = {
        3: f"Stage 3: EoA classify  ->  {result.label}",
        4: f"Stage 4: Body center  ->  pipette target  [{result.label}]",
    }
    text = stage_labels.get(stage, f"Stage {stage}")

    line_h = int(cv2.getTextSize("A", font, scale, thick)[0][1] * 2.5)
    # Overlay at the bottom so it doesn't collide with the existing banner
    y = out.shape[0] - 10
    cv2.rectangle(
        out,
        (0, y - line_h - 10), (int(out.shape[1] * 0.7), y + 5),
        (20, 20, 20), -1,
    )
    colour = (0, 200, 0) if result.label == "BUG" else (128, 128, 128)
    cv2.putText(out, text, (10, y - 5), font, scale, colour, thick)

    return out


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------

def _show(title: str, img: np.ndarray) -> None:
    """Resize and display an image, wait for keypress."""
    scale = DISPLAY_HEIGHT / img.shape[0]
    disp = cv2.resize(img, (int(img.shape[1] * scale), DISPLAY_HEIGHT))
    cv2.imshow(f"{title}  [any key = next]", disp)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def _save(out_dir: Path, name: str, img: np.ndarray) -> None:
    """Write an annotated image to disk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    cv2.imwrite(str(path), img)
    log.info("Saved: %s", path.name)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    overhead_path: Path,
    eoa_path: Path,
    save_dir: Path,
    headless: bool = False,
) -> dict:
    """Run all four pipeline stages and return a result summary dict.

    Args:
        overhead_path: Path to the overhead camera image.
        eoa_path:      Path to the EoA arm camera image.
        save_dir:      Directory for annotated output images.
        headless:      If True, skip cv2.imshow windows.

    Returns:
        Dict with keys: bugs_detected, selected_px, eoa_label, body_center_px.
    """
    summary: dict = {}

    # -- Stage 1: Overhead detection -------------------------------------------
    log.info("=" * 60)
    log.info("Stage 1 | Overhead detection: %s", overhead_path.name)
    overhead_result = run_overhead_detection(overhead_path)

    if overhead_result.error:
        log.error("Overhead detection failed: %s", overhead_result.error)
        sys.exit(1)

    n_bugs = overhead_result.bug_count
    centroids = overhead_result.centroids_px
    log.info("  -> %d bug(s) detected in dish", n_bugs)
    summary["bugs_detected"] = n_bugs

    if n_bugs == 0:
        log.warning("No bugs found in overhead image. Nothing to pick.")
        summary["selected_px"] = None
        summary["eoa_label"] = "NONE"
        summary["body_center_px"] = None
        return summary

    # -- Stage 2: Bug selection ------------------------------------------------
    log.info("Stage 2 | Bug selection (corner-first traversal)")
    selected, remaining = select_bug(
        centroids,
        overhead_result.dish_cx,
        overhead_result.dish_cy,
        overhead_result.dish_radius,
    )
    log.info(
        "  -> Selected: (%.0f, %.0f) px  |  %d remaining",
        selected[0], selected[1], len(remaining),
    )
    summary["selected_px"] = selected

    # Annotate overhead image
    overhead_ann = annotate_overhead(
        overhead_path, centroids, selected,
        overhead_result.dish_cx, overhead_result.dish_cy, overhead_result.dish_radius,
    )
    _save(save_dir, f"stage1_2_overhead_{overhead_path.stem}.jpg", overhead_ann)
    if not headless:
        _show("Stage 1+2: Overhead detection + selection", overhead_ann)

    print(f"\n{'-' * 60}")
    print(f"  Stage 1: {n_bugs} bug(s) detected in overhead image")
    print(f"  Stage 2: Selected bug at pixel ({selected[0]:.0f}, {selected[1]:.0f})")
    print(f"           [Robot should move EoA to this position]")
    print(f"{'-' * 60}\n")

    # -- Stage 3: EoA classification -------------------------------------------
    log.info("Stage 3 | EoA classification: %s", eoa_path.name)
    eoa_result = classify_eoa_image(eoa_path, save_dir=None)
    summary["eoa_label"] = eoa_result.label

    eoa_ann = annotate_eoa_stage(eoa_result, stage=3)
    _save(save_dir, f"stage3_eoa_{eoa_path.stem}_{eoa_result.label}.jpg", eoa_ann)
    if not headless:
        _show(f"Stage 3: EoA classify -> {eoa_result.label}", eoa_ann)

    print(f"  Stage 3: EoA image classified as  ->  {eoa_result.label}")
    if eoa_result.label != "BUG":
        print(f"           Reason: {eoa_result.notes}")
        print(f"           [No-go: robot should not attempt pick]")
        summary["body_center_px"] = None
        return summary

    # -- Stage 4: Body center --------------------------------------------------
    log.info("Stage 4 | Body center / pipette target")
    body_center = eoa_result.body_center_px
    summary["body_center_px"] = body_center

    eoa_ann4 = annotate_eoa_stage(eoa_result, stage=4)
    _save(save_dir, f"stage4_body_{eoa_path.stem}.jpg", eoa_ann4)
    if not headless:
        _show("Stage 4: Body center (pipette target)", eoa_ann4)

    if body_center is not None:
        print(f"  Stage 4: Pipette target (body center):")
        print(f"           Pixel  : ({body_center[0]:.1f}, {body_center[1]:.1f}) px")
        print(f"           [Apply homography here to get robot XY mm]")
    else:
        print("  Stage 4: Body center could not be computed (no dark body pixels)")

    print(f"{'-' * 60}\n")

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CIB001 — End-to-End Vision Pipeline Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Runs all 4 pipeline stages on saved images.\n"
            "Stage 1: overhead detect  Stage 2: select bug\n"
            "Stage 3: EoA classify     Stage 4: body center (pipette target)"
        ),
    )
    parser.add_argument(
        "--overhead", "-o", required=True,
        help="Overhead camera image path (backlit dish with bugs)",
    )
    parser.add_argument(
        "--eoa", "-e", required=True,
        help="EoA arm camera image path (single specimen, backlit)",
    )
    parser.add_argument(
        "--save", "-s", default=None,
        help="Directory for annotated output images (default: ./results/pipeline/)",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Skip live display windows (headless / batch mode)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    overhead_path = Path(args.overhead)
    eoa_path = Path(args.eoa)

    for p in (overhead_path, eoa_path):
        if not p.exists():
            log.error("File not found: %s", p)
            sys.exit(1)

    save_dir = Path(args.save) if args.save else _HERE / "results" / "pipeline"

    print(f"\n{'=' * 60}")
    print(f"  CIB001 Vision Pipeline Demo")
    print(f"  Overhead : {overhead_path.name}")
    print(f"  EoA      : {eoa_path.name}")
    print(f"{'=' * 60}\n")

    summary = run_pipeline(
        overhead_path=overhead_path,
        eoa_path=eoa_path,
        save_dir=save_dir,
        headless=args.no_show,
    )

    print(f"\n{'=' * 60}")
    print(f"  Pipeline summary")
    print(f"  Bugs in dish      : {summary.get('bugs_detected', 0)}")
    sel = summary.get("selected_px")
    if sel:
        print(f"  Selected (px)     : ({sel[0]:.0f}, {sel[1]:.0f})")
    print(f"  EoA result        : {summary.get('eoa_label', 'N/A')}")
    bc = summary.get("body_center_px")
    if bc:
        print(f"  Pipette target    : ({bc[0]:.1f}, {bc[1]:.1f}) px")
    else:
        print(f"  Pipette target    : N/A (no bug confirmed)")
    print(f"  Saved to          : {save_dir}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
