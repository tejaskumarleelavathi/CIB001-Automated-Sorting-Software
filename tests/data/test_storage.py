"""
Tests for cib/data/storage.py
"""

import csv
from datetime import datetime
from pathlib import Path

import pytest

from cib.data.models import Pick, PickResult
from cib.data.storage import RunStorage, CSV_COLUMNS


def make_pick(well: str, result: str = PickResult.SUCCESS) -> Pick:
    return Pick(
        run_id="run_20260616_143022",
        field_code="CIB-2026-001",
        plate_id="PLATE-A",
        well_position=well,
        result=result,
        timestamp_start=datetime(2026, 6, 16, 14, 31, 5),
        timestamp_end=datetime(2026, 6, 16, 14, 31, 8),
        image_filename=f"eoa_{well.lower()}.tiff",
    )


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_creates_file_and_header(tmp_path):
    storage = RunStorage.__new__(RunStorage)
    storage._run_id = "run_test"
    storage._run_dir = tmp_path / "run_test"
    storage._run_dir.mkdir()
    storage._csv_path = storage._run_dir / "run_test_picks.csv"
    storage._file = open(storage._csv_path, "a", newline="", encoding="utf-8")
    import csv as _csv
    storage._writer = _csv.DictWriter(storage._file, fieldnames=CSV_COLUMNS)
    storage._writer.writeheader()
    storage._file.flush()

    storage.close()

    rows = read_csv(storage._csv_path)
    assert rows == []  # header only, no data rows yet
    # Verify header is correct by reading raw
    with open(storage._csv_path) as f:
        header_line = f.readline().strip()
    assert "field_code" in header_line
    assert "well_position" in header_line
    assert "image_filename" in header_line


def test_append_single_pick(tmp_path):
    with RunStorage.__new__(RunStorage) as storage:
        # Patch paths to use tmp_path
        storage._run_id = "run_20260616_143022"
        storage._run_dir = tmp_path / "run_20260616_143022"
        storage._run_dir.mkdir()
        storage._csv_path = storage._run_dir / "run_20260616_143022_picks.csv"

        import csv as _csv
        file_exists = False
        storage._file = open(storage._csv_path, "a", newline="", encoding="utf-8")
        storage._writer = _csv.DictWriter(storage._file, fieldnames=CSV_COLUMNS)
        if not file_exists:
            storage._writer.writeheader()
            storage._file.flush()

        storage.append_pick(make_pick("A1"))

    rows = read_csv(tmp_path / "run_20260616_143022" / "run_20260616_143022_picks.csv")
    assert len(rows) == 1
    assert rows[0]["well_position"] == "A1"
    assert rows[0]["field_code"] == "CIB-2026-001"
    assert rows[0]["result"] == PickResult.SUCCESS
    assert rows[0]["image_filename"] == "eoa_a1.tiff"


def test_full_storage_integration(tmp_path):
    """Use RunStorage normally via its __init__, but with patched _BASE_DIR."""
    import cib.data.storage as storage_mod
    original_base = storage_mod._BASE_DIR
    storage_mod._BASE_DIR = tmp_path

    try:
        run_id = "run_20260616_143022"
        with RunStorage(run_id) as storage:
            for well in ["A1", "A2", "A3"]:
                storage.append_pick(make_pick(well))
            # One failure
            fail_pick = make_pick("A4", result=PickResult.FAIL)
            fail_pick.error_code = "NO_SPECIMEN"
            fail_pick.error_message = "No specimen detected"
            storage.append_pick(fail_pick)

        csv_path = tmp_path / run_id / f"{run_id}_picks.csv"
        rows = read_csv(csv_path)

        assert len(rows) == 4
        assert [r["well_position"] for r in rows] == ["A1", "A2", "A3", "A4"]
        assert rows[3]["result"] == PickResult.FAIL
        assert rows[3]["error_code"] == "NO_SPECIMEN"
        assert rows[3]["error_message"] == "No specimen detected"

    finally:
        storage_mod._BASE_DIR = original_base


def test_resume_appends_not_overwrites(tmp_path):
    """Re-opening RunStorage on an existing file appends rows, doesn't re-write header."""
    import cib.data.storage as storage_mod
    original_base = storage_mod._BASE_DIR
    storage_mod._BASE_DIR = tmp_path

    try:
        run_id = "run_20260616_143022"

        # First session: write A1, A2
        with RunStorage(run_id) as s:
            s.append_pick(make_pick("A1"))
            s.append_pick(make_pick("A2"))

        # Second session (resume after crash): write A3
        with RunStorage(run_id) as s:
            s.append_pick(make_pick("A3"))

        csv_path = tmp_path / run_id / f"{run_id}_picks.csv"
        rows = read_csv(csv_path)
        assert len(rows) == 3
        assert [r["well_position"] for r in rows] == ["A1", "A2", "A3"]

    finally:
        storage_mod._BASE_DIR = original_base
