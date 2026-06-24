"""
cib/data/database.py — SQLite persistence for runs, picks, and safety events.

One SQLite file holds the entire history: C:\\LabAutomation\\data\\picks.db

Key design decisions (see architecture/data-layer.md):
- Every pick is written immediately when it completes — not batched at end of run.
- Run status starts as 'in_progress' the moment START is pressed.
- On startup, check for orphaned in_progress runs and offer resume.
- All DB access is synchronous; callers must run in a thread (run_in_executor).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from cib.data.models import Pick, PickResult, PickStatus, Run, RunStatus, SafetyEvent


DB_PATH = Path(r"C:\LabAutomation\data\picks.db")

# DDL — idempotent, run once on startup
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    field_code  TEXT NOT NULL,
    started_at  DATETIME NOT NULL,
    ended_at    DATETIME,
    status      TEXT NOT NULL DEFAULT 'in_progress',
    total_picks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS picks (
    pick_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    field_code       TEXT NOT NULL,
    plate_id         TEXT NOT NULL,
    well_position    TEXT NOT NULL,
    result           TEXT NOT NULL DEFAULT 'in_progress',
    pick_status      TEXT,
    timestamp_start  DATETIME,
    timestamp_end    DATETIME,
    image_filename   TEXT,
    robot_x          REAL,
    robot_y          REAL,
    error_code       TEXT,
    error_message    TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS safety_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    triggered_at DATETIME NOT NULL,
    cleared_at   DATETIME,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""


class Database:
    """
    Thread-safe SQLite wrapper.

    Uses a per-thread connection (check_same_thread=False + threading.local)
    so that multiple asyncio worker threads can each hold their own connection
    without sharing a connection object across threads.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path),
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads during writes
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        self._conn().executescript(_SCHEMA)
        self._conn().commit()

    @staticmethod
    def _dt(val) -> Optional[datetime]:
        """Coerce a sqlite3 value to datetime (handles str or None)."""
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(str(val))

    # ── Run operations ────────────────────────────────────────────────────────

    def create_run(self, run: Run) -> None:
        """Insert a new run row. Status is 'in_progress' by definition."""
        self._conn().execute(
            """
            INSERT INTO runs (run_id, field_code, started_at, ended_at, status, total_picks)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.field_code,
                run.started_at.isoformat(),
                run.ended_at.isoformat() if run.ended_at else None,
                run.status,
                run.total_picks,
            ),
        )
        self._conn().commit()

    def finish_run(self, run_id: str, status: str, total_picks: int) -> None:
        """Mark a run completed/aborted/faulted and record the end time."""
        self._conn().execute(
            """
            UPDATE runs
            SET status = ?, ended_at = ?, total_picks = ?
            WHERE run_id = ?
            """,
            (status, datetime.now().isoformat(), total_picks, run_id),
        )
        self._conn().commit()

    def get_run(self, run_id: str) -> Optional[Run]:
        row = self._conn().execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def find_incomplete_run(self) -> Optional[Run]:
        """
        Return the most recent run with status='in_progress', or None.
        Used on startup to detect crash/power-loss resume scenario.
        """
        row = self._conn().execute(
            "SELECT * FROM runs WHERE status = 'in_progress' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return self._row_to_run(row) if row else None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            field_code=row["field_code"],
            started_at=Database._dt(row["started_at"]),
            ended_at=Database._dt(row["ended_at"]),
            status=row["status"],
            total_picks=row["total_picks"],
        )

    # ── Pick operations ───────────────────────────────────────────────────────

    def start_pick(self, pick: Pick) -> int:
        """
        Insert a pick row with result='in_progress', pick_status='IMAGING_OVERHEAD'.
        Returns the auto-assigned pick_id.
        This is called the moment the pick cycle begins.
        If power cuts here, we know which well was in-flight and what step it was on.
        """
        cur = self._conn().execute(
            """
            INSERT INTO picks
                (run_id, field_code, plate_id, well_position, result, pick_status,
                 timestamp_start, image_filename, robot_x, robot_y)
            VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?)
            """,
            (
                pick.run_id,
                pick.field_code,
                pick.plate_id,
                pick.well_position,
                pick.pick_status or PickStatus.IMAGING_OVERHEAD,
                pick.timestamp_start.isoformat() if pick.timestamp_start else None,
                pick.image_filename,
                pick.robot_x,
                pick.robot_y,
            ),
        )
        self._conn().commit()
        return cur.lastrowid

    def update_pick_status(self, pick_id: int, pick_status: str) -> None:
        """
        Update the pick_status column as the pick cycle progresses.
        Called at every step transition — imaging, moving, aspirating, etc.
        This is what lets resume-after-crash know exactly where the robot was.
        """
        self._conn().execute(
            "UPDATE picks SET pick_status = ? WHERE pick_id = ?",
            (pick_status, pick_id),
        )
        self._conn().commit()

    def finish_pick(self, pick_id: int, result: str,
                    timestamp_end: Optional[datetime] = None,
                    error_code: Optional[str] = None,
                    error_message: Optional[str] = None) -> None:
        """
        Update a pick row once the dispense completes (or fails).
        Called immediately when the pick resolves — not batched.
        """
        self._conn().execute(
            """
            UPDATE picks
            SET result = ?, timestamp_end = ?, error_code = ?, error_message = ?
            WHERE pick_id = ?
            """,
            (
                result,
                timestamp_end.isoformat() if timestamp_end else None,
                error_code,
                error_message,
                pick_id,
            ),
        )
        self._conn().commit()

    def get_completed_picks(self, run_id: str) -> List[Pick]:
        """Return all successful picks for a run, ordered by pick_id."""
        rows = self._conn().execute(
            """
            SELECT * FROM picks
            WHERE run_id = ? AND result = 'success'
            ORDER BY pick_id ASC
            """,
            (run_id,),
        ).fetchall()
        return [self._row_to_pick(r) for r in rows]

    def get_interrupted_pick(self, run_id: str) -> Optional[Pick]:
        """
        Return the pick with result='in_progress' for a run, if any.
        This is the pick that was in-flight when the crash happened.
        """
        row = self._conn().execute(
            "SELECT * FROM picks WHERE run_id = ? AND result = 'in_progress' LIMIT 1",
            (run_id,),
        ).fetchone()
        return self._row_to_pick(row) if row else None

    def mark_pick_interrupted(self, pick_id: int) -> None:
        """Mark an in-flight pick as failed due to interruption (crash/power loss)."""
        self.finish_pick(
            pick_id=pick_id,
            result=PickResult.FAIL,
            timestamp_end=datetime.now(),
            error_code="INTERRUPTED",
            error_message="Pick was in progress when power was cut or software crashed",
        )

    @staticmethod
    def _row_to_pick(row: sqlite3.Row) -> Pick:
        return Pick(
            pick_id=row["pick_id"],
            run_id=row["run_id"],
            field_code=row["field_code"],
            plate_id=row["plate_id"],
            well_position=row["well_position"],
            result=row["result"],
            pick_status=row["pick_status"],
            timestamp_start=Database._dt(row["timestamp_start"]),
            timestamp_end=Database._dt(row["timestamp_end"]),
            image_filename=row["image_filename"],
            robot_x=row["robot_x"],
            robot_y=row["robot_y"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    # ── Safety event operations ───────────────────────────────────────────────

    def record_safety_event(self, event: SafetyEvent) -> int:
        """Insert a safety event row. Returns the auto-assigned event_id."""
        cur = self._conn().execute(
            """
            INSERT INTO safety_events (run_id, event_type, triggered_at, cleared_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.event_type,
                event.triggered_at.isoformat(),
                event.cleared_at.isoformat() if event.cleared_at else None,
            ),
        )
        self._conn().commit()
        return cur.lastrowid

    def clear_safety_event(self, event_id: int) -> None:
        """Record when a safety event was cleared (operator pressed reset)."""
        self._conn().execute(
            "UPDATE safety_events SET cleared_at = ? WHERE event_id = ?",
            (datetime.now().isoformat(), event_id),
        )
        self._conn().commit()

    # ── Resume logic ──────────────────────────────────────────────────────────

    def get_resume_state(self, run_id: str, power_cut: bool = False) -> dict:
        """
        Compute everything the orchestrator needs to resume a run after a crash.

        `power_cut=True` when there is no uncleared safety event in the DB —
        meaning the interruption was a full power loss or software crash, not
        an e-stop / safety curtain. This changes the recovery action for picks
        where the specimen may have been in the pipette.

        Returns:
            {
                "completed_wells":  ["A1", "A2", "A3"],
                "interrupted_pick": Pick | None,
                "recovery_action":  "RETRY" | "DISPENSE_AT_WELL" | "OPERATOR_CHECK" | None,
                "operator_message": str | None,
            }

        Recovery actions:
            RETRY           — specimen was not in pipette; retry the full pick cycle
            DISPENSE_AT_WELL — e-stop/curtain; specimen is in pipette; send robot to well
            OPERATOR_CHECK  — power cut; specimen may be lost; operator decides
        """
        completed = self.get_completed_picks(run_id)
        interrupted = self.get_interrupted_pick(run_id)

        recovery_action = None
        operator_message = None

        if interrupted is not None:
            status = interrupted.pick_status
            well = interrupted.well_position

            if status in PickStatus.RETRY_SAFE or status is None:
                # Specimen was still in the dish — safe to retry
                recovery_action = "RETRY"
                operator_message = (
                    f"Well {well} was interrupted before the specimen was picked. "
                    f"Retry the pick for {well}?"
                )

            elif status in PickStatus.SPECIMEN_IN_PIPETTE:
                if not power_cut:
                    # E-stop or safety curtain — linear motor held; specimen in pipette
                    recovery_action = "DISPENSE_AT_WELL"
                    operator_message = (
                        f"Specimen is in the pipette (interrupted at: {status}). "
                        f"SPEL+ program must be restarted first (teach pendant or HMI). "
                        f"Press DISPENSE to send robot to Well {well}, "
                        f"or SKIP to discard this specimen."
                    )
                else:
                    # Power cut — linear motor lost power; specimen location unknown
                    recovery_action = "OPERATOR_CHECK"
                    operator_message = (
                        f"Power was cut while specimen was in pipette (step: {status}). "
                        f"Specimen may have been lost during robot rehoming. "
                        f"Check pipette tip and Well {well} manually, then press CONTINUE or SKIP."
                    )

        return {
            "completed_wells":  [p.well_position for p in completed],
            "interrupted_pick": interrupted,
            "recovery_action":  recovery_action,
            "operator_message": operator_message,
        }

    def has_uncleared_safety_event(self, run_id: str) -> bool:
        """
        Returns True if there is a safety event (e-stop / light curtain) for this
        run that has not been cleared. Used to distinguish e-stop interruption
        from power cut on resume.
        """
        row = self._conn().execute(
            """
            SELECT 1 FROM safety_events
            WHERE run_id = ? AND cleared_at IS NULL
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
