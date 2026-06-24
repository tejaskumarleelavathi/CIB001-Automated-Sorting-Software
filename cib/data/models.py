"""
cib/data/models.py — Python dataclasses for runs, picks, and safety events.

These are the in-memory representations. database.py persists them to SQLite.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Optional


# ── Result / status literals ───────────────────────────────────────────────────

class PickResult:
    SUCCESS     = "success"
    FAIL        = "fail"
    IN_PROGRESS = "in_progress"


class RunStatus:
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    ABORTED     = "aborted"
    FAULTED     = "faulted"


class ErrorCode:
    NO_SPECIMEN = "NO_SPECIMEN"
    INTERRUPTED = "INTERRUPTED"      # power cut / crash while pick was in flight
    ROBOT_FAULT = "ROBOT_FAULT"
    TIMEOUT     = "TIMEOUT"


class PickStatus:
    """
    Fine-grained step within a single pick cycle.
    Updated in DB at every transition so resume-after-crash knows exactly
    what the robot was doing.

    Critical boundary: MOVING_TO_PICK → ASPIRATING.
    Before ASPIRATING: specimen is in the sorting dish.
    From ASPIRATING onward: specimen may be in the pipette.
    """
    IMAGING_OVERHEAD        = "IMAGING_OVERHEAD"
    MOVING_EOA_CAMERA       = "MOVING_EOA_CAMERA"
    IMAGING_EOA             = "IMAGING_EOA"
    COMPUTING_PICK_LOCATION = "COMPUTING_PICK_LOCATION"
    MOVING_TO_PICK          = "MOVING_TO_PICK"
    ASPIRATING              = "ASPIRATING"       # treat same as BUG_IN_PIPETTE on interrupt
    BUG_IN_PIPETTE          = "BUG_IN_PIPETTE"  # specimen aspirated, robot moving to well
    DISPENSING              = "DISPENSING"
    DONE                    = "DONE"

    # Statuses where the specimen is definitely NOT in the pipette yet
    # → safe to retry the full pick cycle
    RETRY_SAFE = {
        IMAGING_OVERHEAD,
        MOVING_EOA_CAMERA,
        IMAGING_EOA,
        COMPUTING_PICK_LOCATION,
        MOVING_TO_PICK,
    }

    # Statuses where specimen may be in the pipette
    # → need operator decision before resuming
    SPECIMEN_IN_PIPETTE = {
        ASPIRATING,
        BUG_IN_PIPETTE,
        DISPENSING,
    }


class SafetyEventType:
    ESTOP         = "estop"
    LIGHT_CURTAIN = "light_curtain"


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Run:
    """One row in the `runs` table — corresponds to one operator-initiated run."""

    run_id:      str                    # e.g. "run_20260616_143022"
    field_code:  str                    # e.g. "CIB-2026-001"
    started_at:  datetime
    ended_at:    Optional[datetime] = None
    status:      str = RunStatus.IN_PROGRESS
    total_picks: int = 0


@dataclasses.dataclass
class Pick:
    """One row in the `picks` table — corresponds to one specimen pick attempt."""

    run_id:           str
    field_code:       str
    plate_id:         str               # e.g. "PLATE-A"
    well_position:    str               # e.g. "A1"
    result:           str = PickResult.IN_PROGRESS
    pick_status:      Optional[str] = None   # PickStatus.* — updated at every step
    timestamp_start:  Optional[datetime] = None
    timestamp_end:    Optional[datetime] = None
    image_filename:   Optional[str] = None
    robot_x:          Optional[float] = None
    robot_y:          Optional[float] = None
    error_code:       Optional[str] = None
    error_message:    Optional[str] = None
    pick_id:          Optional[int] = None   # set by DB after INSERT


@dataclasses.dataclass
class SafetyEvent:
    """One row in the `safety_events` table."""

    run_id:       str
    event_type:   str               # SafetyEventType.ESTOP / LIGHT_CURTAIN
    triggered_at: datetime
    cleared_at:   Optional[datetime] = None
    event_id:     Optional[int] = None   # set by DB after INSERT
