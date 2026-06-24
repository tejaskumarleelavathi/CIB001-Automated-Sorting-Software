"""
Tests for cib/data/database.py

Uses an in-memory SQLite DB (":memory:") — no files created on disk.
"""

import pytest
from datetime import datetime
from pathlib import Path

from cib.data.database import Database
from cib.data.models import Pick, PickResult, PickStatus, Run, RunStatus, SafetyEvent, SafetyEventType


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """In-memory-ish DB using a temp file so threading.local works cleanly."""
    d = Database(db_path=tmp_path / "test_picks.db")
    yield d
    d.close()


def make_run(run_id="run_20260616_143022", field_code="CIB-2026-001") -> Run:
    return Run(
        run_id=run_id,
        field_code=field_code,
        started_at=datetime(2026, 6, 16, 14, 30, 22),
    )


def make_pick(run_id="run_20260616_143022", well="A1") -> Pick:
    return Pick(
        run_id=run_id,
        field_code="CIB-2026-001",
        plate_id="PLATE-A",
        well_position=well,
        timestamp_start=datetime(2026, 6, 16, 14, 31, 5),
    )


# ── Run tests ─────────────────────────────────────────────────────────────────

def test_create_and_get_run(db):
    run = make_run()
    db.create_run(run)
    fetched = db.get_run(run.run_id)
    assert fetched is not None
    assert fetched.run_id == "run_20260616_143022"
    assert fetched.field_code == "CIB-2026-001"
    assert fetched.status == RunStatus.IN_PROGRESS
    assert fetched.ended_at is None


def test_finish_run_completed(db):
    db.create_run(make_run())
    db.finish_run("run_20260616_143022", RunStatus.COMPLETED, total_picks=48)
    run = db.get_run("run_20260616_143022")
    assert run.status == RunStatus.COMPLETED
    assert run.total_picks == 48
    assert run.ended_at is not None


def test_finish_run_aborted(db):
    db.create_run(make_run())
    db.finish_run("run_20260616_143022", RunStatus.ABORTED, total_picks=12)
    run = db.get_run("run_20260616_143022")
    assert run.status == RunStatus.ABORTED


def test_find_incomplete_run_none_when_clean(db):
    """No incomplete run on a fresh DB."""
    assert db.find_incomplete_run() is None


def test_find_incomplete_run_found(db):
    db.create_run(make_run())
    found = db.find_incomplete_run()
    assert found is not None
    assert found.run_id == "run_20260616_143022"


def test_find_incomplete_run_ignores_completed(db):
    db.create_run(make_run())
    db.finish_run("run_20260616_143022", RunStatus.COMPLETED, total_picks=48)
    assert db.find_incomplete_run() is None


def test_find_incomplete_run_returns_most_recent(db):
    r1 = make_run("run_20260616_100000")
    r1.started_at = datetime(2026, 6, 16, 10, 0, 0)
    r2 = make_run("run_20260616_110000")
    r2.started_at = datetime(2026, 6, 16, 11, 0, 0)
    db.create_run(r1)
    db.create_run(r2)
    found = db.find_incomplete_run()
    assert found.run_id == "run_20260616_110000"


# ── Pick tests ────────────────────────────────────────────────────────────────

def test_start_pick_returns_id(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    assert isinstance(pick_id, int)
    assert pick_id >= 1


def test_start_pick_result_is_in_progress(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    interrupted = db.get_interrupted_pick("run_20260616_143022")
    assert interrupted is not None
    assert interrupted.pick_id == pick_id
    assert interrupted.result == PickResult.IN_PROGRESS
    assert interrupted.well_position == "A1"


def test_finish_pick_success(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    db.finish_pick(pick_id, PickResult.SUCCESS, timestamp_end=datetime(2026, 6, 16, 14, 31, 8))
    # No longer interrupted
    assert db.get_interrupted_pick("run_20260616_143022") is None
    # Appears in completed picks
    completed = db.get_completed_picks("run_20260616_143022")
    assert len(completed) == 1
    assert completed[0].well_position == "A1"
    assert completed[0].result == PickResult.SUCCESS


def test_finish_pick_fail_with_error(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="B3"))
    db.finish_pick(
        pick_id,
        PickResult.FAIL,
        timestamp_end=datetime(2026, 6, 16, 14, 32, 0),
        error_code="NO_SPECIMEN",
        error_message="Vision found no specimen at this position",
    )
    completed = db.get_completed_picks("run_20260616_143022")
    assert completed == []   # fail doesn't count as completed


def test_mark_pick_interrupted(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="C4"))
    db.mark_pick_interrupted(pick_id)
    # get_interrupted_pick should now return None (no longer in_progress)
    assert db.get_interrupted_pick("run_20260616_143022") is None


def test_get_completed_picks_ordered(db):
    db.create_run(make_run())
    wells = ["A1", "A2", "A3", "B1"]
    for well in wells:
        pid = db.start_pick(make_pick(well=well))
        db.finish_pick(pid, PickResult.SUCCESS, timestamp_end=datetime.now())
    completed = db.get_completed_picks("run_20260616_143022")
    assert [p.well_position for p in completed] == wells


# ── Resume logic ──────────────────────────────────────────────────────────────

def test_resume_state_clean(db):
    """Clean run (no picks started) — nothing to resume."""
    db.create_run(make_run())
    state = db.get_resume_state("run_20260616_143022")
    assert state["completed_wells"] == []
    assert state["interrupted_pick"] is None


def test_resume_state_after_partial_run(db):
    """3 picks completed, 1 in progress (power cut scenario)."""
    db.create_run(make_run())
    for well in ["A1", "A2", "A3"]:
        pid = db.start_pick(make_pick(well=well))
        db.finish_pick(pid, PickResult.SUCCESS, timestamp_end=datetime.now())
    # Simulate crash mid-pick on A4
    db.start_pick(make_pick(well="A4"))

    state = db.get_resume_state("run_20260616_143022")
    assert state["completed_wells"] == ["A1", "A2", "A3"]
    assert state["interrupted_pick"] is not None
    assert state["interrupted_pick"].well_position == "A4"


# ── Safety events ─────────────────────────────────────────────────────────────

def test_record_and_clear_safety_event(db):
    db.create_run(make_run())
    event = SafetyEvent(
        run_id="run_20260616_143022",
        event_type=SafetyEventType.ESTOP,
        triggered_at=datetime(2026, 6, 16, 14, 35, 0),
    )
    event_id = db.record_safety_event(event)
    assert isinstance(event_id, int)
    # Clear it
    db.clear_safety_event(event_id)
    # Verify via raw conn that cleared_at is set
    row = db._conn().execute(
        "SELECT cleared_at FROM safety_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row["cleared_at"] is not None


# ── pick_status tests ─────────────────────────────────────────────────────────

def test_start_pick_default_status_is_imaging_overhead(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    interrupted = db.get_interrupted_pick("run_20260616_143022")
    assert interrupted.pick_status == PickStatus.IMAGING_OVERHEAD


def test_update_pick_status(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    db.update_pick_status(pick_id, PickStatus.MOVING_EOA_CAMERA)
    interrupted = db.get_interrupted_pick("run_20260616_143022")
    assert interrupted.pick_status == PickStatus.MOVING_EOA_CAMERA


def test_update_pick_status_through_full_cycle(db):
    db.create_run(make_run())
    pick_id = db.start_pick(make_pick(well="A1"))
    for status in [
        PickStatus.MOVING_EOA_CAMERA,
        PickStatus.IMAGING_EOA,
        PickStatus.COMPUTING_PICK_LOCATION,
        PickStatus.MOVING_TO_PICK,
        PickStatus.ASPIRATING,
        PickStatus.BUG_IN_PIPETTE,
        PickStatus.DISPENSING,
        PickStatus.DONE,
    ]:
        db.update_pick_status(pick_id, status)
    db.finish_pick(pick_id, PickResult.SUCCESS, timestamp_end=datetime.now())
    completed = db.get_completed_picks("run_20260616_143022")
    assert completed[0].pick_status == PickStatus.DONE


# ── resume state with recovery action tests ───────────────────────────────────

def test_resume_state_retry_safe_statuses(db):
    """When interrupted before ASPIRATING, recovery action is RETRY."""
    db.create_run(make_run())
    for well in ["A1", "A2"]:
        pid = db.start_pick(make_pick(well=well))
        db.finish_pick(pid, PickResult.SUCCESS, timestamp_end=datetime.now())

    pid = db.start_pick(make_pick(well="A3"))
    db.update_pick_status(pid, PickStatus.MOVING_TO_PICK)

    state = db.get_resume_state("run_20260616_143022", power_cut=False)
    assert state["recovery_action"] == "RETRY"
    assert state["interrupted_pick"].well_position == "A3"
    assert "A3" in state["operator_message"]


def test_resume_state_estop_with_bug_in_pipette(db):
    """E-stop while BUG_IN_PIPETTE → DISPENSE_AT_WELL."""
    db.create_run(make_run())
    pid = db.start_pick(make_pick(well="B2"))
    db.update_pick_status(pid, PickStatus.BUG_IN_PIPETTE)

    # Record an uncleared e-stop so has_uncleared_safety_event returns True
    db.record_safety_event(SafetyEvent(
        run_id="run_20260616_143022",
        event_type=SafetyEventType.ESTOP,
        triggered_at=datetime.now(),
    ))

    assert db.has_uncleared_safety_event("run_20260616_143022") is True
    state = db.get_resume_state("run_20260616_143022", power_cut=False)
    assert state["recovery_action"] == "DISPENSE_AT_WELL"
    assert "B2" in state["operator_message"]
    assert "SPEL+" in state["operator_message"]


def test_resume_state_power_cut_with_bug_in_pipette(db):
    """Power cut while BUG_IN_PIPETTE → OPERATOR_CHECK."""
    db.create_run(make_run())
    pid = db.start_pick(make_pick(well="C5"))
    db.update_pick_status(pid, PickStatus.BUG_IN_PIPETTE)

    # No safety event in DB → power cut
    assert db.has_uncleared_safety_event("run_20260616_143022") is False
    state = db.get_resume_state("run_20260616_143022", power_cut=True)
    assert state["recovery_action"] == "OPERATOR_CHECK"
    assert "C5" in state["operator_message"]
    assert "pipette" in state["operator_message"].lower()


def test_resume_state_aspirating_treated_as_specimen_in_pipette(db):
    """ASPIRATING is ambiguous — treated same as BUG_IN_PIPETTE."""
    db.create_run(make_run())
    pid = db.start_pick(make_pick(well="D1"))
    db.update_pick_status(pid, PickStatus.ASPIRATING)

    state = db.get_resume_state("run_20260616_143022", power_cut=False)
    assert state["recovery_action"] == "DISPENSE_AT_WELL"


def test_has_uncleared_safety_event_false_when_cleared(db):
    db.create_run(make_run())
    event_id = db.record_safety_event(SafetyEvent(
        run_id="run_20260616_143022",
        event_type=SafetyEventType.LIGHT_CURTAIN,
        triggered_at=datetime.now(),
    ))
    db.clear_safety_event(event_id)
    assert db.has_uncleared_safety_event("run_20260616_143022") is False
