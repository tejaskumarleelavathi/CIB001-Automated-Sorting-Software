#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_methods.py — Side-by-side Comparison of Bug Detection Methods
======================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Runs both detection methods on every image in the overhead_images directory,
saves annotated results, and prints a comparison table so you can decide
which method to promote to the production pipeline.

Method A — Global Threshold     (method_a_global_threshold.py)
Method B — CLAHE + Adaptive     (method_b_adaptive_clahe.py)

Output layout:
    software_tests/overhead_detection/
        results/
            method_a/   ← annotated images from Method A
            method_b/   ← annotated images from Method B

Usage:
    python compare_methods.py                         # default image dir
    python compare_methods.py path/to/images/         # custom folder
    python compare_methods.py --no-show               # headless batch
    python compare_methods.py --single path/img.jpg   # one image side by side
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2
import numpy as np

# Local method modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
import method_a_global_threshold as method_a
import method_b_adaptive_clahe as method_b

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cib.compare")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISPLAY_HEIGHT: int = 900        # Window height for side-by-side view
SUPPORTED_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png")


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _side_by_side(
    img_a: np.ndarray,
    img_b: np.ndarray,
    label_a: str = "Method A — Global Threshold",
    label_b: str = "Method B — CLAHE + Adaptive",
) -> np.ndarray:
    """Combine two annotated images into a single side-by-side panel.

    Both images are scaled to the same height before concatenation.
    A divider line and header labels are added for clarity.

    Args:
        img_a: Annotated result from Method A (BGR).
        img_b: Annotated result from Method B (BGR).
        label_a: Caption for the left panel.
        label_b: Caption for the right panel.

    Returns:
        Combined BGR image.
    """
    target_h = DISPLAY_HEIGHT
    scale_a = target_h / img_a.shape[0]
    scale_b = target_h / img_b.shape[0]

    small_a = cv2.resize(img_a, (int(img_a.shape[1] * scale_a), target_h))
    small_b = cv2.resize(img_b, (int(img_b.shape[1] * scale_b), target_h))

    # Add label bar at the top of each panel
    def _add_label(panel: np.ndarray, text: str) -> np.ndarray:
        bar = np.zeros((40, panel.shape[1], 3), dtype=np.uint8)
        cv2.putText(bar, text, (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 220, 100), 2)
        return np.vstack([bar, panel])

    panel_a = _add_label(small_a, label_a)
    panel_b = _add_label(small_b, label_b)

    # Vertical divider
    divider = np.full((panel_a.shape[0], 4, 3), (80, 80, 80), dtype=np.uint8)

    return np.hstack([panel_a, divider, panel_b])


def _print_table(rows: list[dict]) -> None:
    """Print a formatted comparison table to stdout.

    Args:
        rows: List of dicts with keys: name, count_a, count_b, error_a, error_b.
    """
    col_w = 32
    print(f"\n{'=' * 72}")
    print(f"  {'IMAGE':<{col_w}}  {'METHOD A':>10}  {'METHOD B':>10}  {'DIFF':>6}")
    print(f"  {'-' * col_w}  {'-' * 10}  {'-' * 10}  {'-' * 6}")

    total_a = total_b = 0
    for r in rows:
        ca = r["count_a"]
        cb = r["count_b"]
        err_a = "ERR" if r["error_a"] else str(ca)
        err_b = "ERR" if r["error_b"] else str(cb)
        diff = "" if (r["error_a"] or r["error_b"]) else f"{cb - ca:+d}"
        print(f"  {r['name']:<{col_w}}  {err_a:>10}  {err_b:>10}  {diff:>6}")
        if not r["error_a"]:
            total_a += ca
        if not r["error_b"]:
            total_b += cb

    print(f"  {'-' * col_w}  {'-' * 10}  {'-' * 10}  {'-' * 6}")
    n = len(rows)
    print(f"  {'TOTAL':<{col_w}}  {total_a:>10}  {total_b:>10}  {total_b - total_a:>+6}")
    print(f"  {'AVERAGE':<{col_w}}  {total_a/n:>10.1f}  {total_b/n:>10.1f}")
    print(f"{'=' * 72}\n")
    print("  Positive DIFF means Method B found MORE bugs than Method A.")
    print("  Negative DIFF means Method B found FEWER bugs than Method A.\n")


# ---------------------------------------------------------------------------
# Main comparison logic
# ---------------------------------------------------------------------------

def run_comparison(
    image_paths: list[Path],
    save_dir: Path,
    show: bool = True,
) -> None:
    """Run both methods on every image and produce a comparison report.

    Annotated images are saved to save_dir/method_a/ and save_dir/method_b/.
    A side-by-side composite is saved to save_dir/comparison/.

    Args:
        image_paths: Ordered list of image paths to process.
        save_dir: Root output directory.
        show: Whether to display each side-by-side composite on screen.
    """
    dir_a = save_dir / "method_a"
    dir_b = save_dir / "method_b"
    dir_cmp = save_dir / "comparison"

    for d in (dir_a, dir_b, dir_cmp):
        d.mkdir(parents=True, exist_ok=True)

    table_rows: list[dict] = []

    for img_path in image_paths:
        log.info("Comparing methods on: %s", img_path.name)

        result_a = method_a.run_detection(img_path)
        result_b = method_b.run_detection(img_path)

        table_rows.append({
            "name": img_path.name,
            "count_a": result_a.bug_count,
            "count_b": result_b.bug_count,
            "error_a": result_a.error,
            "error_b": result_b.error,
        })

        # Save individual annotated images
        if result_a.annotated_image is not None:
            out_a = dir_a / f"A_{img_path.stem}_detected{img_path.suffix}"
            cv2.imwrite(str(out_a), result_a.annotated_image)

        if result_b.annotated_image is not None:
            out_b = dir_b / f"B_{img_path.stem}_detected{img_path.suffix}"
            cv2.imwrite(str(out_b), result_b.annotated_image)

        # Save + optionally show side-by-side
        if result_a.annotated_image is not None and result_b.annotated_image is not None:
            try:
                composite = _side_by_side(
                    result_a.annotated_image,
                    result_b.annotated_image,
                    label_a=f"Method A  |  {result_a.bug_count} bugs",
                    label_b=f"Method B  |  {result_b.bug_count} bugs",
                )
                out_cmp = dir_cmp / f"CMP_{img_path.name}"
                cv2.imwrite(str(out_cmp), composite)

                if show:
                    win_title = (
                        f"{img_path.name}  |  A={result_a.bug_count}  "
                        f"B={result_b.bug_count}  (any key = next)"
                    )
                    cv2.imshow(win_title, composite)
                    cv2.waitKey(0)
                    cv2.destroyAllWindows()

            except Exception as exc:  # noqa: BLE001
                log.error("Failed to build composite for %s: %s", img_path.name, exc)

    _print_table(table_rows)
    log.info("Results saved to: %s", save_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare bug detection Method A vs Method B (CIB001)"
    )
    parser.add_argument(
        "input", nargs="?",
        help="Image folder (default: CIB_Software/output/overhead_images)",
    )
    parser.add_argument(
        "--single", metavar="IMAGE",
        help="Run comparison on a single image file only.",
    )
    parser.add_argument(
        "--save", "-s", default=None,
        help="Output directory for results (default: next to this script in results/)",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Skip live display — useful for headless/batch mode.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point: compare both methods across all images."""
    args = _parse_args()

    default_images = (
        Path(__file__).resolve().parents[2] / "output" / "overhead_images"
    )
    default_save = Path(__file__).resolve().parent / "results"

    save_dir = Path(args.save) if args.save else default_save

    # Resolve image list
    if args.single:
        single = Path(args.single)
        if not single.is_file():
            log.error("--single path is not a file: %s", single)
            sys.exit(1)
        paths = [single]

    else:
        input_dir = Path(args.input) if args.input else default_images
        if not input_dir.is_dir():
            log.error("'%s' is not a directory.", input_dir)
            sys.exit(1)

        paths = sorted(
            p for p in input_dir.iterdir()
            if p.suffix.lower() in SUPPORTED_EXTS
        )

        if not paths:
            log.error("No supported images found in: %s", input_dir)
            sys.exit(1)

    print(f"\n{'=' * 72}")
    print(f"  CIB001 Bug Detection — Method Comparison")
    print(f"  Images : {len(paths)}")
    print(f"  Output : {save_dir}")
    print(f"{'=' * 72}")

    try:
        run_comparison(paths, save_dir, show=not args.no_show)
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        cv2.destroyAllWindows()
        sys.exit(0)


if __name__ == "__main__":
    main()
