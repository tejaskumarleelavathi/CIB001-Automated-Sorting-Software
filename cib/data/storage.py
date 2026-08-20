"""
cib/data/storage.py — Per-run CSV writer.

Writes one row per completed pick, appended immediately (not buffered).
If power goes out mid-run, the CSV contains all completed picks up to that point.

Output path: C:\\LabAutomation\\data\\runs\\{run_id}\\{run_id}_picks.csv

CSV columns (confirmed with Austin for LIMS import):
  field_code, plate_id, plate_position, well_position, image_filename,
  timestamp_start, timestamp_end, result, error_code, error_message
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from cib.data.models import Pick


# Columns in the exact order the LIMS import expects
CSV_COLUMNS = [
    "field_code",
    "plate_id",
    "plate_position",   # same as well_position; LIMS uses this header name
    "well_position",
    "image_filename",
    "timestamp_start",
    "timestamp_end",
    "result",
    "error_code",
    "error_message",
]

_BASE_DIR = Path(r"C:\LabAutomation\data\runs")


class RunStorage:
    """
    Opens (or re-opens) the CSV file for a given run_id and appends rows.

    Usage:
        storage = RunStorage(run_id)
        storage.append_pick(pick)   # called once per completed pick
        storage.close()             # called when run ends
    """

    def __init__(self, run_id: str, base_dir: Path | None = None) -> None:
        self._run_id = run_id
        self._run_dir = (base_dir if base_dir is not None else _BASE_DIR) / run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._csv_path = self._run_dir / f"{run_id}_picks.csv"

        # Open in append mode so resume-after-crash continues the same file
        file_exists = self._csv_path.exists()
        self._file = open(self._csv_path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_COLUMNS)

        # Write header only if this is a new file
        if not file_exists:
            self._writer.writeheader()
            self._file.flush()

    def append_pick(self, pick: Pick) -> None:
        """
        Append one pick row to the CSV.
        Called immediately when a pick completes — not at end of run.
        """
        self._writer.writerow({
            "field_code":      pick.field_code,
            "plate_id":        pick.plate_id,
            "plate_position":  pick.well_position,  # LIMS column name alias
            "well_position":   pick.well_position,
            "image_filename":  pick.image_filename or "",
            "timestamp_start": _fmt(pick.timestamp_start),
            "timestamp_end":   _fmt(pick.timestamp_end),
            "result":          pick.result,
            "error_code":      pick.error_code or "",
            "error_message":   pick.error_message or "",
        })
        # Flush to disk immediately — don't buffer
        self._file.flush()

    @property
    def csv_path(self) -> Path:
        return self._csv_path

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.close()

    def __enter__(self) -> "RunStorage":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _fmt(dt: Optional[datetime]) -> str:
    return dt.isoformat(sep=" ", timespec="seconds") if dt else ""
