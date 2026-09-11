"""
hardware_tests/verify_eoa_labels.py — visually verify eoa_labels.csv

Sanity-check for the Halcon dataset labeling added to
cib/core/orchestrator.py's _save_eoa_frames() (2026-09-01): for every
with-backlight EoA image that has a row in eoa_labels.csv, draws a
crosshair at the recorded (pixel_x, pixel_y) -- the bug/specimen position,
from the image's own top-left origin, NOT robot coordinates -- so you can
eyeball whether it actually lands on the bug.

Only walks images that have a CSV row (not every file in the folder --
the folder may contain older captures from before labeling existed).
Pure viewer: never writes/overwrites any file. Navigate with the
keyboard, quit any time.

Usage:
    python hardware_tests/verify_eoa_labels.py
    python hardware_tests/verify_eoa_labels.py --dir cib/output/eoa_images/backlight_on
    python hardware_tests/verify_eoa_labels.py --csv path/to/eoa_labels.csv
    python hardware_tests/verify_eoa_labels.py --from-ts 20260907_164648_933
        (only rows with a timestamp >= this one -- e.g. to review everything
        from a specific row in the CSV onward, nothing before it)

Controls:
    n / Right arrow  -> next image
    p / Left arrow   -> previous image
    q / Esc          -> quit
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2

DEFAULT_DIR = Path(__file__).parent.parent / "cib" / "output" / "eoa_images" / "backlight_on"

# Display window is capped to this so a full-res GigE capture (often
# several thousand px wide) still fits on a normal monitor -- scaling is
# applied uniformly so the drawn crosshair still lands in the right place.
MAX_DISPLAY_W = 1400
MAX_DISPLAY_H = 900


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def draw_crosshair(img, x: float, y: float, size: int = 30, color=(0, 0, 255)) -> None:
    x, y = int(round(x)), int(round(y))
    thickness = max(2, img.shape[1] // 600)
    cv2.line(img, (x - size, y), (x + size, y), color, thickness)
    cv2.line(img, (x, y - size), (x, y + size), color, thickness)
    cv2.circle(img, (x, y), size // 3, color, thickness)


def render(row: dict, img_dir: Path, index: int, total: int):
    img_path = img_dir / row["image_filename"]
    img = cv2.imread(str(img_path))
    if img is None:
        # Still show *something* so navigation doesn't just silently stall
        # on a missing file (moved/deleted image, stale CSV row, etc.).
        import numpy as np
        img = 30 * np.ones((500, 900, 3), dtype="uint8")
        cv2.putText(img, f"MISSING FILE: {row['image_filename']}", (20, 250),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        return img

    px, py = row.get("pixel_x", ""), row.get("pixel_y", "")
    has_coord = px not in ("", None) and py not in ("", None)
    if has_coord:
        draw_crosshair(img, float(px), float(py))

    h, w = img.shape[:2]
    scale = min(MAX_DISPLAY_W / w, MAX_DISPLAY_H / h, 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    label = row.get("label", "?")
    reason = row.get("reason", "")
    coord_txt = f"({px}, {py})" if has_coord else "no coordinate (no specimen found)"
    lines = [
        f"[{index + 1}/{total}]  {row['image_filename']}",
        f"label={label}   pixel={coord_txt}",
        f"reason: {reason}",
        "n/-> next   p/<- prev   q/Esc quit",
    ]
    for i, line in enumerate(lines):
        y0 = 22 + i * 22
        cv2.putText(img, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)
    return img


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                     help=f"Folder containing the images and eoa_labels.csv (default: {DEFAULT_DIR})")
    ap.add_argument("--csv", type=Path, default=None,
                     help="Explicit path to eoa_labels.csv (default: <dir>/eoa_labels.csv)")
    ap.add_argument("--from-ts", type=str, default=None,
                     help="Only show rows with timestamp >= this value (e.g. "
                          "20260907_164648_933, from the 'timestamp' column). "
                          "Nothing before it is shown. The timestamp format "
                          "sorts correctly as a plain string, so this is a "
                          "simple >= comparison, not a date parse.")
    args = ap.parse_args()

    csv_path = args.csv or (args.dir / "eoa_labels.csv")
    if not csv_path.exists():
        print(f"[ERROR] Not found: {csv_path}")
        return

    rows = load_rows(csv_path)
    if not rows:
        print(f"[ERROR] {csv_path} has no rows.")
        return
    print(f"[OK] Loaded {len(rows)} labeled image(s) from {csv_path}")

    if args.from_ts:
        total_before = len(rows)
        rows = [r for r in rows if r.get("timestamp", "") >= args.from_ts]
        print(f"[OK] Filtered to {len(rows)}/{total_before} row(s) with "
              f"timestamp >= {args.from_ts}")
        if not rows:
            print(f"[ERROR] No rows have a timestamp >= {args.from_ts} -- "
                  f"check the value matches the CSV's 'timestamp' column exactly.")
            return

    idx = 0
    window = "verify_eoa_labels"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)

    while True:
        img = render(rows[idx], args.dir, idx, len(rows))
        cv2.imshow(window, img)
        key = cv2.waitKey(0) & 0xFF

        if key in (ord("q"), 27):  # q or Esc
            break
        elif key in (ord("n"), 83):  # n or Right arrow
            idx = min(idx + 1, len(rows) - 1)
        elif key in (ord("p"), 81):  # p or Left arrow
            idx = max(idx - 1, 0)
        # anything else: just redraw the same image

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
