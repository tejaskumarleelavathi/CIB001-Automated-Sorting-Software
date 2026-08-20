"""
diagnose_eoa_20260811.py — run the REAL cib/vision/eoa.py pipeline on the 3
EoA images saved during today's (2026-08-11) main.py run, and print full
per-blob diagnostics (not just the final label) so we can see exactly which
gate rejected each one.

Does NOT modify any cib/ code — only imports and calls the existing
functions. Read-only diagnostic script.

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" software_tests/diagnose_eoa_20260811.py
"""

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cib.vision import eoa as eoa_mod  # noqa: E402

IMAGES = [
    "cib/output/eoa_images/backlight_on/eoa_with_backlight_20260811_103446_421.jpg",
    "cib/output/eoa_images/backlight_on/eoa_with_backlight_20260811_103518_390.jpg",
    "cib/output/eoa_images/backlight_on/eoa_with_backlight_20260811_103546_847.jpg",
]

OUT_DIR = Path(__file__).resolve().parent / "results" / "eoa_20260811"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def diagnose(img_path: Path) -> None:
    print("\n" + "=" * 78)
    print(f"  {img_path.name}")
    print("=" * 78)

    frame = cv2.imread(str(img_path))
    if frame is None:
        print(f"  [ERROR] could not read {img_path}")
        return

    gray = eoa_mod._to_gray(frame)
    h, w = gray.shape[:2]

    binary, thresh = eoa_mod._segment(gray)
    fg_coverage = float((binary > 0).sum()) / binary.size
    print(f"  Otsu threshold       : {thresh:.1f}")
    print(f"  Foreground coverage  : {fg_coverage:.1%}  (limit {eoa_mod.MAX_FG_COVERAGE:.0%})")

    if fg_coverage > eoa_mod.MAX_FG_COVERAGE:
        print("  -> REJECTED at coverage guard (bad lighting / overexposed)")
        return

    original_binary = binary.copy()
    closed = eoa_mod._morph_close(binary)
    blobs = eoa_mod._find_blobs(closed)
    print(f"  Blobs found (>={eoa_mod.MIN_CONTOUR_AREA}px^2): {len(blobs)}")

    if not blobs:
        print("  -> REJECTED: no blobs found after segmentation")
        return

    ranked = eoa_mod._rank_candidates(blobs, gray.shape)

    print(f"\n  {'#':<3}{'label':<8}{'diam':>6}{'sol':>6}{'ar':>6}{'dark%':>7}{'chroma':>8}{'defects':>9}  reason")
    print("  " + "-" * 90)

    best_label = "DEBRIS"
    for i, blob in enumerate(ranked):
        eoa_mod._intensity_features(blob, gray, frame, original_binary)
        label, reason = eoa_mod._classify(blob)
        print(f"  {i:<3}{label:<8}{blob['equiv_diameter']:>6.0f}{blob['solidity']:>6.2f}"
              f"{blob['aspect_ratio']:>6.2f}{blob['dark_pixel_ratio']*100:>6.0f}%"
              f"{blob['mean_chroma_variance']:>8.1f}{blob['significant_defects']:>9d}  {reason}")
        if label == "BUG":
            best_label = "BUG"
            body_cx, body_cy, body_mask = eoa_mod._body_center(blob, gray, original_binary)
            annotated = eoa_mod._annotate(frame, blob, label, (body_cx, body_cy), body_mask, 0.0)
            out_path = OUT_DIR / f"{img_path.stem}_BUG.jpg"
            cv2.imwrite(str(out_path), annotated)
            print(f"\n  Saved annotated: {out_path}")
            break
    else:
        # No BUG found — annotate the top-ranked (most central) blob for review
        top = ranked[0]
        eoa_mod._intensity_features(top, gray, frame, original_binary)
        label, _ = eoa_mod._classify(top)
        annotated = eoa_mod._annotate(frame, top, label, None, None, 0.0)
        out_path = OUT_DIR / f"{img_path.stem}_{label}.jpg"
        cv2.imwrite(str(out_path), annotated)
        print(f"\n  Saved annotated (most central blob, NOT a bug): {out_path}")

    print(f"\n  FINAL RESULT: {best_label}")


def main() -> None:
    for rel in IMAGES:
        p = Path(__file__).resolve().parent.parent / rel
        diagnose(p)


if __name__ == "__main__":
    main()
