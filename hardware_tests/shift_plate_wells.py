"""
shift_plate_wells.py -- apply a fixed X/Y correction to every well in a
plate config Excel file, in place.

Why this exists (2026-08-19, see DEV-13 in prd_updated.md):
Tool 2's pipette offset in sort_server.prg was corrected by (-1mm X, -2mm Y)
to fix PICK landing off the specimen. Since DROP uses the SAME Tool 2 (a
short-lived separate DROP-only tool offset was tried and reverted), the well
X/Y values recorded in the plate config -- taught using the OLD, uncorrected
offset -- are now off by exactly that same delta. This script applies the
identical shift to the config so DROP lands back at the true well positions.

Derivation (not a guess): the OLD offset was confirmed correct for DROP
(that's why a DROP-only tool using the old offset worked), and the NEW
offset is confirmed correct for PICK. Shifting the recorded well X/Y by the
same delta applied to the tool offset (new_offset - old_offset) makes the
NEW offset land at the TRUE well position instead of the recorded one.

Usage:
  python hardware_tests/shift_plate_wells.py <path.xlsx> --dx -1 --dy -2
  python hardware_tests/shift_plate_wells.py <path.xlsx> --dx -1 --dy -2 --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import openpyxl


def shift_plate_wells(path: Path, dx: float, dy: float, dry_run: bool = False) -> int:
    wb = openpyxl.load_workbook(str(path))
    ws = wb.active

    rows = list(ws.iter_rows())
    if not rows:
        print("[ERROR] Sheet is empty.")
        return 0

    headers = [str(c.value).strip().lower() if c.value is not None else "" for c in rows[0]]
    try:
        x_col = headers.index("x")
        y_col = headers.index("y")
        well_col = headers.index("well") if "well" in headers else None
    except ValueError:
        print(f"[ERROR] Could not find 'x'/'y' columns. Headers found: {headers}")
        return 0

    n_shifted = 0
    for row in rows[1:]:
        if all(c.value is None or str(c.value).strip() == "" for c in row):
            continue
        x_cell = row[x_col]
        y_cell = row[y_col]
        if x_cell.value is None or y_cell.value is None:
            continue

        old_x, old_y = float(x_cell.value), float(y_cell.value)
        new_x, new_y = old_x + dx, old_y + dy

        label = str(row[well_col].value) if well_col is not None and row[well_col].value else "?"
        print(f"  {label:>4s}:  x {old_x:9.3f} -> {new_x:9.3f}   y {old_y:9.3f} -> {new_y:9.3f}")

        if not dry_run:
            x_cell.value = new_x
            y_cell.value = new_y
        n_shifted += 1

    if dry_run:
        print(f"\n[DRY RUN] Would shift {n_shifted} wells by (dx={dx}, dy={dy}). No file written.")
        return n_shifted

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    print(f"\n[BACKUP] Original saved to {backup}")

    wb.save(str(path))
    print(f"[OK] Shifted {n_shifted} wells by (dx={dx}, dy={dy}) and saved to {path}")
    return n_shifted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="Plate config .xlsx file to shift, in place")
    ap.add_argument("--dx", type=float, required=True, help="mm to add to every well's x")
    ap.add_argument("--dy", type=float, required=True, help="mm to add to every well's y")
    ap.add_argument("--dry-run", action="store_true", help="Print the shift without writing the file")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"[ERROR] File not found: {args.path}")
        sys.exit(1)

    shift_plate_wells(args.path, args.dx, args.dy, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
