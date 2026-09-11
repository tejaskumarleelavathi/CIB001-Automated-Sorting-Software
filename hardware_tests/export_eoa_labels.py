"""
hardware_tests/export_eoa_labels.py — export a slice of eoa_labels.csv (and
its images) to a separate folder, for sharing (e.g. with David).

Companion to verify_eoa_labels.py: use that one first to eyeball a cutoff
row's crosshair and confirm it's the right starting point
(`python hardware_tests/verify_eoa_labels.py --from-ts <timestamp>`), then
run this one with the same --from-ts to actually copy that slice out.

Copies, into a NEW folder (never touches the source files):
    - every image from --from-ts onward (by the CSV's own 'timestamp' column)
    - a trimmed eoa_labels.csv containing only those rows (same header)

Usage:
    python hardware_tests/export_eoa_labels.py --from-ts 20260907_164648_933 --out cib/output/for_david
    python hardware_tests/export_eoa_labels.py --from-ts 20260907_164648_933 --out cib/output/for_david --dir cib/output/eoa_images/backlight_on
"""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent.parent / "cib" / "output" / "eoa_images" / "backlight_on"


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-ts", type=str, required=True,
                     help="Only export rows with timestamp >= this value "
                          "(e.g. 20260907_164648_933, from the CSV's "
                          "'timestamp' column). Nothing before it is copied.")
    ap.add_argument("--out", type=Path, required=True,
                     help="Destination folder to create/fill (must not "
                          "already exist, so an old export is never "
                          "silently overwritten).")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                     help=f"Folder containing the images and eoa_labels.csv (default: {DEFAULT_DIR})")
    ap.add_argument("--csv", type=Path, default=None,
                     help="Explicit path to eoa_labels.csv (default: <dir>/eoa_labels.csv)")
    args = ap.parse_args()

    csv_path = args.csv or (args.dir / "eoa_labels.csv")
    if not csv_path.exists():
        print(f"[ERROR] Not found: {csv_path}")
        return

    if args.out.exists():
        print(f"[ERROR] {args.out} already exists -- pick a new/empty folder "
              f"so an old export is never silently overwritten.")
        return

    rows = load_rows(csv_path)
    if not rows:
        print(f"[ERROR] {csv_path} has no rows.")
        return

    subset = [r for r in rows if r.get("timestamp", "") >= args.from_ts]
    print(f"[OK] {len(subset)}/{len(rows)} row(s) have timestamp >= {args.from_ts}")
    if not subset:
        print(f"[ERROR] No rows have a timestamp >= {args.from_ts} -- "
              f"check the value matches the CSV's 'timestamp' column exactly.")
        return

    args.out.mkdir(parents=True)

    copied = 0
    missing: list[str] = []
    for row in subset:
        src = args.dir / row["image_filename"]
        if not src.exists():
            missing.append(row["image_filename"])
            continue
        shutil.copy2(src, args.out / src.name)
        copied += 1

    out_csv = args.out / "eoa_labels.csv"
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(subset[0].keys()))
        writer.writeheader()
        writer.writerows(subset)

    print(f"[OK] Copied {copied} image(s) and {out_csv.name} ({len(subset)} row(s)) to {args.out}")
    if missing:
        print(f"[WARN] {len(missing)} row(s) pointed at a missing image file (skipped, "
              f"still included in the CSV export so the row isn't silently lost):")
        for name in missing:
            print(f"        {name}")


if __name__ == "__main__":
    main()
