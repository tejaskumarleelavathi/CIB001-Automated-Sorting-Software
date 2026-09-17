"""
Orchestrator — the central brain.

Runs the asyncio event loop. Owns one worker coroutine per device.
All device workers put events onto a single queue. The orchestrator
reads from that queue and feeds the state machine. The state machine
decides what to do. The orchestrator executes it.

RULES (enforced by structure, not just comments):
  - This file is the ONLY place that calls driver methods.
  - The UI never calls drivers directly.
  - Device workers never call each other.
  - All decisions go through the state machine.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import structlog

from cib.config.loader import PlateConfig, PlateStationsConfig
from cib.data.database import Database
from cib.data.storage import RunStorage
from cib.data.models import (
    Pick,
    PickResult,
    PickStatus,
    Run,
    RunStatus,
    SafetyEvent,
    SafetyEventType,
)
from cib.core.events import (
    AllDevicesReadyEvent,
    ConnectionLostEvent,
    CyclePhase,
    DeviceName,
    EoaFrameCapturedEvent,
    FrameReadyEvent,
    CameraID,
    OperatorAbortEvent,
    OperatorAcknowledgeEvent,
    OperatorConfigurePreRunEvent,
    OperatorShutdownEvent,
    OperatorStartEvent,
    OperatorStopEvent,
    PLCEventLogEntry,
    PumpAction,
    PumpActionCompleteEvent,
    RecoveryStatusEvent,
    RobotClearedFOVEvent,
    RobotInPositionEvent,
    RobotMissionCompleteEvent,
    RunStatusEvent,
    SafetyFaultEvent,
    SafetyFaultType,
    SafetyStatusEvent,
    SystemErrorEvent,
    SystemEvent,
    VisionEoAResultEvent,
    VisionOverheadResultEvent,
)
from cib.core.state_machine import State, SystemStateMachine
from cib.drivers.base import (
    CameraDriver,
    LightingDriver,
    PLCDriver,
    PumpDriver,
    RobotDriver,
    RobotRemoteDriver,
    SafetyDriver,
    StatusLight,
)

log = structlog.get_logger(__name__)

SAFETY_POLL_INTERVAL       = 0.10   # 100ms
PLC_POLL_INTERVAL          = 0.10   # 100ms

# 2026-09-11, found live: this installation's light curtain read flickers
# between clear/tripped even with nothing in the beam (confirmed on real
# hardware -- the log showed it alternating for minutes at a stretch). A
# single instantaneous "clear right now" poll is not trustworthy enough to
# gate a reset-acknowledge attempt or the CONTINUE button on -- both need
# the curtain to have read clear continuously for this long first. See
# _safety_poll_loop()'s light-curtain debounce tracking. Starting value;
# widen if real flicker turns out to be longer-period than this, but if
# CONTINUE still won't stay enabled even with a generous window, that's a
# sign of a genuine physical curtain problem (alignment, dust, a reflective
# surface nearby) to go check, not something more software can paper over.
LIGHT_CURTAIN_STABLE_CLEAR_S = 2.0
ROBOT_POLL_INTERVAL        = 0.20   # 200ms
ROBOT_KEEPALIVE_INTERVAL_S = 0.5    # seconds — Epson TCP idle timer fires at ~1 s; 0.5s keeps well under it

# Consecutive failed keepalive ticks (each ROBOT_KEEPALIVE_INTERVAL_S apart)
# before attempting the heavier Remote Ethernet restart -- the driver's own
# lightweight per-tick reconnect (see driver.py's keepalive()) already
# handles a single transient blip; this only escalates once that's clearly
# not working. ROBOT_KEEPALIVE_RECOVERY_COOLDOWN_S then rate-limits repeat
# attempts of the heavier sequence so a still-broken connection doesn't get
# hammered with a fresh Stop/Reset/SetMotorsOn/Start every single tick.
ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD = 3
ROBOT_KEEPALIVE_RECOVERY_COOLDOWN_S = 10.0

# _safe_recovery()'s Remote Ethernet fallback retry count/delay (2026-08-31,
# real-hardware finding): a safety fault landing right in the middle of a
# transient network blip can make the FIRST full restart attempt fail for
# purely timing reasons -- observed live where CONTINUE went straight to
# FAULT, but a subsequent ABORT's plain reconnect succeeded moments later
# with nothing else different except the elapsed time. Retrying here, same
# as the recovery a manual ABORT-then-STOP-then-ABORT was accidentally
# providing, means CONTINUE alone should now succeed without that detour.
SAFE_RECOVERY_REMOTE_ETHERNET_RETRIES = 3
SAFE_RECOVERY_RETRY_DELAY_S = 3.0

# Substrings that mark a _run_cycle() exception as robot-comms-related (vs.
# e.g. a camera-only error) — gates whether Remote Ethernet recovery is
# attempted. Checked case-insensitively. Plain "robot" alone is NOT enough:
# cib/drivers/robot/driver.py raises several exact "<COMMAND> failed: ..."
# messages (MOVE_IMAGE/PICK/DROP/HOME/GO_HOME_LEFT/RING_ON/RING_OFF) that
# contain no "robot" substring at all — each is listed explicitly here so
# none of them are silently missed.
ROBOT_ERROR_MARKERS = (
    "robot",
    "MOVE_IMAGE failed", "PICK failed", "DROP failed", "HOME failed",
    "GO_HOME_LEFT failed", "RING_ON failed", "RING_OFF failed",
)

# Max time to wait, after a drop finishes, for the lookahead overhead
# capture (fired concurrently with that same drop) to produce a target —
# see _run_cycle()'s "Lookahead" step. In the common case this is ~0s: the
# lookahead is fired right as the drop starts and drop takes several
# seconds, so the capture is usually already done by the time we check.
# This is just a safety cap for the uncommon case (slow camera response) —
# past this, give up and home normally rather than stall the cycle.
LOOKAHEAD_TIMEOUT_S = 3.0

# A centroid gets this many real pick attempts (across separate overhead
# captures) before being permanently excluded for the rest of the run
# (2026-09-14, operator request: a blob that's still sitting there after
# one attempt may just have been missed/rejected once, not genuinely
# unpickable — give it one more try before giving up on it). Does NOT
# apply within a single overhead capture's own EoA-reject retry loop
# (_select_next_target() reusing the same all_centroids list) -- a
# candidate just rejected in THIS capture must never be re-selected
# again in the same capture regardless of this count, or the retry loop
# would just pick the same rejected spot straight back.
MAX_ATTEMPTS_PER_CENTROID = 2

# Fine correction applied to the vision-computed PICK target only (2026-08-19,
# see DEV-15 in prd_updated.md) — deliberately NOT in TLSet 2 (sort_server.prg)
# and NOT in the plate config, after those approaches spent a full session
# chasing what turned out to be a vision-calibration bug (DEV-14), not a tool-
# offset one, and kept leaking the "fix" into DROP in the process. Living here
# means it can only ever affect PICK. Re-tune/zero out once real picks confirm
# whether it's still needed after the DEV-14 per-quadrant EoA fix.
PICK_TARGET_CORRECTION_X_MM = -1.0
PICK_TARGET_CORRECTION_Y_MM = 0.0

# Additional PICK correction for Lefty hand only, ON TOP of
# PICK_TARGET_CORRECTION_X_MM/_Y_MM above (2026-09-17, operator hardware
# report: picking up the bug from the dish lands ~1mm off in -X/-Y
# specifically when the cycle is using the Lefty hand -- i.e. whenever the
# target well is on a "left"-side station (plate 3/4); Righty picks are
# fine). PICK_TARGET_CORRECTION_X_MM/_Y_MM itself was tuned 2026-08-19,
# a week before Lefty/multi-station sorting existed (2026-08-26) -- like
# TLSet 2 (see the DROP-side investigation this same day), it was only
# ever validated against Righty picks. This is a single hardware report,
# not a full calibration -- re-tune after more real Lefty picks confirm
# it. Lives here, added only for hand=="L", so Righty PICK and every DROP
# stay exactly as they were.
PICK_LEFT_EXTRA_CORRECTION_X_MM = 1.0
PICK_LEFT_EXTRA_CORRECTION_Y_MM = 1.0


@dataclass
class DriverSet:
    """All hardware drivers injected into the orchestrator."""
    robot:            RobotDriver
    robot_remote:     RobotRemoteDriver   # controller lifecycle control (Reset/Start/
                                           # SetMotorsOn), separate from robot's own
                                           # PICK/DROP protocol. See cib/drivers/robot_remote/.
    safety:           SafetyDriver
    plc:              PLCDriver
    pump:             PumpDriver
    camera_overhead:  CameraDriver
    camera_eoa:       CameraDriver
    lighting:         LightingDriver


class Orchestrator:
    """
    Wires together: drivers, state machine, event queue, and UI bridge.

    Instantiate with real drivers for production, mock drivers for development.
    The rest of the codebase never knows the difference.
    """

    # Tolerance (mm) for "same centroid" comparison across frames.
    # Visited centroid tracking — radius-based Euclidean distance check.
    # A centroid is considered visited if it falls within centroid_snap_mm of
    # any previously attempted centroid. Stores actual float coords (not snapped)
    # so the check is not sensitive to grid-cell boundary artefacts.
    # Calibration RMSE = 0.043mm so inter-frame noise is <<0.5mm — same physical
    # spot reliably lands within the radius across cycles.
    #
    # Future: replace simple visited set with a per-centroid state machine:
    #   PICKED     → picked successfully, remove from dish model
    #   DEBRIS     → EoA confirmed not a bug, never revisit
    #   RETRY      → EoA rejected but unsure, retry at end (max 3 attempts)
    #   FAILED     → retried 3 times, no pick, alert operator
    # See architecture/vision.md § Bug Tracking State Machine.

    def __init__(
        self,
        drivers: DriverSet,
        on_state_change: Callable[[State], None] | None = None,
        on_event: Callable[[SystemEvent], None] | None = None,
        overhead_save_dir: Path | None = None,
        overhead_save_format: str = "png",
        overhead_save_images: bool = True,
        unprocessed_overhead_save_dir: Path | None = None,
        eoa_save_dir: Path | None = None,
        force_sample_detection: bool = False,
        view: bool = False,
        centroid_snap_mm: float = 0.8,
        eoa_normal_exposure_us: float | None = None,
        eoa_backlight_on_exposure_us: float | None = None,
        quadrant_split_y_mm: float | None = None,
        u_top_deg: float | None = None,
        u_bottom_deg: float | None = None,
        plate_config: PlateConfig | None = None,
        plate_u: float | None = None,
        plate_stations_cfg: PlateStationsConfig | None = None,
        db_path: Path | None = None,
        runs_base_dir: Path | None = None,
        field_code: str = "DEV",
        default_plate_id: str = "DEV-PLATE",
        robot_remote_enabled: bool = False,
        safety_poll_interval_s: float = SAFETY_POLL_INTERVAL,
        plc_poll_interval_s: float = PLC_POLL_INTERVAL,
    ) -> None:
        self._drivers = drivers
        self._queue: asyncio.Queue[SystemEvent] = asyncio.Queue()
        # Set once d.safety/d.plc are actually connected in _startup() --
        # replaces a fixed sleep() grace period in _safety_poll_loop()/
        # _plc_poll_loop() (2026-08-30 real-hardware finding: the new
        # unconditional Remote Ethernet startup step, or any slow/failing
        # connect before it, can easily take longer than the old 0.5s
        # guess, so the poll loops started hitting "not connected" and
        # spuriously FAULTing before the real connect ever happened).
        self._safety_plc_connected = asyncio.Event()
        self._sm = SystemStateMachine(on_state_change=on_state_change)
        self._on_event = on_event     # hook for UI to observe raw events
        self._running = False
        self._cycle_active = False          # prevents concurrent _run_cycle() tasks
        # Set for the duration of OperatorAbortEvent's own E_STOP/PROTECTIVE_STOP
        # recovery sequence (2026-09-17, real-hardware finding -- see that
        # handler and _robot_keepalive_tick() for the full story). ABORT calls
        # sm.abort() (-> IDLE) BEFORE its own delivery/home sequence finishes,
        # unlike CONTINUE which stays in E_STOP/PROTECTIVE_STOP for its entire
        # recovery and so is already covered by _robot_keepalive_tick()'s
        # existing state check. That gap let the keepalive loop see "IDLE,
        # robot not ready" and launch its OWN concurrent Remote Ethernet
        # recovery attempt while ABORT's was still in flight -- confirmed live,
        # both racing to reconnect the same robot crashed the whole
        # asyncio.TaskGroup ("unhandled errors in a TaskGroup (1 sub-exception)"
        # right after "orchestrator.robot_keepalive_attempting_remote_recovery"
        # fired mid-ABORT). This flag closes that gap without touching the
        # state machine's own transition timing.
        self._robot_recovery_in_progress = False
        # Set the instant OperatorShutdownEvent's handler starts, before it
        # does anything else (2026-09-16, real-hardware finding) -- lets
        # _robot_keepalive_tick() recognize an intentional disconnect (the
        # shutdown handler's own d.robot.disconnect()) and stand down
        # immediately, rather than treating it as a real fault. Previously
        # only _recover_robot_connection() itself checked for a mid-recovery
        # shutdown (logging "...abandoned_shutdown"), but by the time that
        # ran the keepalive tick had already started a recovery attempt
        # that raced the shutdown handler's own robot_remote sequence --
        # and win or lose, the caller (_robot_keepalive_tick) treated an
        # abandoned attempt exactly like a genuinely failed one, faulting
        # the state machine and setting the status light red_chase during
        # what was actually a normal, successful shutdown.
        self._shutting_down = False
        # _robot_keepalive_tick() state (2026-08-30 idle-disconnect recovery) —
        # initialized here, not just in _robot_keepalive_loop(), so the tick
        # method works standalone (e.g. driven directly by tests).
        self._keepalive_consecutive_disconnected = 0
        self._keepalive_last_recovery_attempt = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None   # set in run()
        # Image saving config — passed from main.py via machine_config
        self._overhead_save_dir = overhead_save_dir
        self._overhead_save_format = overhead_save_format
        self._overhead_save_images = overhead_save_images
        # Raw (pre-vision, pre-annotation) overhead frame save directory
        # (None = don't save) -- 2026-09-08, operator request: overhead_save_dir
        # above only ever holds the ANNOTATED frame (dish circle, detection
        # circles, banner text); this is the untouched camera capture, saved
        # separately so it's never mixed in with or overwritten by that one.
        self._unprocessed_overhead_save_dir = unprocessed_overhead_save_dir
        # EoA annotated frame save directory (None = don't save)
        self._eoa_save_dir = eoa_save_dir
        # Dev flag: inject a fake centroid when vision finds nothing (empty dish).
        self._force_sample_detection = force_sample_detection
        # VIEW flag: show image popups for 3s during testing.
        self._view = view
        # Visited centroids — cleared at start of each run. Each entry is
        # {"x", "y", "count", "generation"}: count = how many real pick
        # attempts this spot has had (across separate overhead captures);
        # generation = the _overhead_capture_generation value at its last
        # attempt, used to keep a same-capture EoA-reject retry from ever
        # re-selecting the candidate it just rejected while still allowing
        # a genuinely later capture to give it a second try (see
        # MAX_ATTEMPTS_PER_CENTROID above).
        self._visited_entries: list[dict] = []
        self._overhead_capture_generation: int = 0
        self._centroid_snap_mm = centroid_snap_mm
        # Set to True only after _startup() posts AllDevicesReadyEvent.
        # Prevents the keepalive loop from firing while sort_server.prg is
        # still executing HOME (it can take >5 s to re-enter Line Input after
        # motion completes, which would cause a keepalive STATUS timeout and
        # leave a stale "OK READY" in the TCP buffer for the next real command).
        self._startup_complete = False
        # Live light curtain state — updated every 100ms by safety poll loop.
        # Used to gate the CONTINUE button: must be False before reset-ack is sent.
        self._light_curtain_tripped = False
        # Debounced version of the above (2026-09-11) -- see
        # LIGHT_CURTAIN_STABLE_CLEAR_S's comment for why the raw reading
        # alone isn't trustworthy. _light_curtain_clear_since is the
        # monotonic timestamp of the most recent False->True->False... run
        # of continuously-clear polls starting (None while currently
        # tripped); _light_curtain_stably_clear is the derived "has it been
        # clear for long enough to trust" flag actually used for gating.
        self._light_curtain_clear_since: float | None = None
        self._light_curtain_stably_clear = False
        # Live "any Flexi fault latched" state — updated every 100ms. For
        # E_STOP specifically, this stays True through button-released and
        # awaiting-reset, and only clears once the FULL hardware reset
        # sequence completes (reset button pressed too — bit 10 restored,
        # "back to normal" per driver.py's confirmed bit map). Used to gate
        # CONTINUE for e-stop the same way light_curtain_tripped gates it
        # for the light curtain (FR-SAF-05: reset must fully complete first).
        self._hardware_fault_active = True   # unknown/assume faulted until first poll
        # Current phase of the operating cycle — set throughout _run_cycle().
        # Used by _safe_recovery() to log what was interrupted and decide cleanup.
        self._cycle_phase = CyclePhase.IDLE
        # EoA pixel-scale calibration — loaded lazily, cached per-U (see DEV-13:
        # the calibration is only valid at the U angle it was measured at, since
        # the EoA camera physically rotates with U — u_top_deg/u_bottom_deg each
        # need their own file). Keyed by the exact target_u float; None key =
        # the old single un-quadranted eoa_pixel_scale.json fallback.
        self._eoa_pixel_scale: dict[float | None, dict] = {}
        # EoA dual-exposure: backlight-OFF uses normal_exposure_us (full exposure for
        # classification), backlight-ON uses backlight_on_exposure_us (shorter to avoid
        # overexposure from the bright backlight). Both are None = don't change exposure.
        self._eoa_normal_exposure_us = eoa_normal_exposure_us
        self._eoa_backlight_on_exposure_us = eoa_backlight_on_exposure_us
        # Quadrant-based U (wrist) angle selection — all three None disables it
        # (move_to_image_site is called without u, robot holds current U, same
        # as pre-2026-08-05 behavior). See _select_quadrant_u().
        self._quadrant_split_y_mm = quadrant_split_y_mm
        self._u_top_deg = u_top_deg
        self._u_bottom_deg = u_bottom_deg
        # Plate config for well-drop sequencing (2026-08-11, test-scoped —
        # see prd_updated.md Appendix D for the not-yet-finalized production
        # design). None = fall back to the old hardcoded stub coordinate.
        self._plate_config = plate_config
        self._plate_u = plate_u
        self._next_well_index = 0
        # Multi-station Lefty/Righty sequencing (2026-08-26) -- see
        # configure_pre_run()/_get_next_well_coords()/_stations_exhausted().
        # plate_stations_cfg (home U's, quadrant U's, station->side map) comes
        # from machine_config.yaml, loaded once at startup regardless of which
        # stations get selected for any given run. _selected_stations/
        # _station_order/_plate_ids_by_station are populated per-run by
        # configure_pre_run(), called by the operator (via the pre-run setup
        # screen) before pressing START. _station_order empty = multi-station
        # mode is off -- every method below falls back to the original
        # single-plate/Righty-only behavior.
        self._plate_stations_cfg = plate_stations_cfg
        self._selected_stations: dict[int, PlateConfig] = {}
        self._station_order: list[int] = []
        self._current_station_idx = 0
        self._current_well_idx = 0
        # Station number -> Plate ID text (FR-UI-01), set by
        # configure_pre_run(). Read at Pick-creation time in _run_cycle() --
        # see _current_station_number().
        self._plate_ids_by_station: dict[int, str] = {}
        # Which hand the robot is ACTUALLY, physically in right now --
        # updated only by _home_right()/_home_left() (the sole two places
        # that ever change it), never derived/guessed. "right" is correct
        # at construction time because _startup() always homes Righty
        # unconditionally, regardless of which stations get selected.
        self._current_physical_side: str = "right"
        # Remote Ethernet (controller-level Reset/Start/SetMotorsOn) — off
        # by default because it requires a one-time RC+ setup (Control
        # Device = Remote Ethernet + port/password) that doesn't exist on
        # every controller. See cib/drivers/robot_remote/driver.py and
        # config/machine_config.yaml's robot_remote section. When False,
        # _remote_ethernet_restart_task() is a no-op and behavior is
        # unchanged from before this existed.
        self._robot_remote_enabled = robot_remote_enabled
        # Tracks whether the Remote Ethernet session is currently open
        # (2026-08-30 -- see _remote_ethernet_restart_task's docstring for
        # why this is now held open across the whole run instead of
        # closed right after Start). Only ever set True by
        # _remote_ethernet_restart_task's own connect, cleared by it on
        # failure or by shutdown's explicit disconnect.
        self._robot_remote_connected: bool = False
        # Poll intervals — instance attributes sourced from config (see
        # main.py) instead of the hardcoded module constants, so
        # config/machine_config.yaml's plc.poll_interval_ms/
        # flexi.poll_interval_ms actually take effect.
        self._safety_poll_interval_s = safety_poll_interval_s
        self._plc_poll_interval_s = plc_poll_interval_s
        # Set when ABORT is pressed while RUNNING (2026-08-30) -- lets the
        # in-progress cycle finish gracefully (same as a plain CYCLE STOP)
        # before _finish_stop() also closes the run, rather than an
        # immediate hard stop mid-motion. See OperatorAbortEvent's handler.
        self._abort_after_cycle: bool = False

        # ── Data layer (SQLite + per-run CSV) ──────────────────────────
        # field_code/default_plate_id are placeholders until the real
        # pre-run setup screen exists (PRD FR-SETUP-01) — see prd checklist.
        self._db = Database(db_path) if db_path is not None else Database()
        self._runs_base_dir = runs_base_dir
        self._storage: RunStorage | None = None
        self._current_run: Run | None = None
        self._field_code = field_code
        self._default_plate_id = default_plate_id
        # In-flight pick row (DB) for whatever the cycle is currently doing.
        self._current_pick: Pick | None = None
        # Set the instant a specimen is confirmed aspirated (in the pipette
        # tip), cleared the instant it's confirmed dispensed. If a safety
        # fault interrupts the cycle while this is set, recovery MUST
        # deliver this specimen to (well_x, well_y, well_u) before doing
        # anything else — never lose it, never start a new pick on top of it.
        # Tuple: (well_x, well_y, well_u, well_label, pick_id, hand)
        self._pending_well: tuple[float, float, float | None, str, int | None, str] | None = None
        # DB row id of the currently-open (uncleared) safety event, if any.
        self._current_safety_event_id: int | None = None
        # What state we were in right before a safety fault interrupted us —
        # captured in _evaluate_safety(). PROTECTIVE_STOP recovery uses this
        # to decide whether to resume RUNNING or just settle back to IDLE:
        # if nothing was running when the curtain tripped (e.g. it tripped
        # between cycles), recovery must NOT start picking that was never
        # asked for. Found 2026-08-18 via a live crash — see state_machine.py.
        self._state_before_fault: State | None = None
        # Image file paths saved so far by the CURRENT cycle attempt (reset
        # at the top of every _run_cycle()). If that attempt gets interrupted
        # before a specimen was ever aspirated, these get deleted — they'd
        # otherwise be orphaned files with no completed pick record pointing
        # at them. NOT touched on a normal reject or a normal success — both
        # are complete, legitimate outcomes, not interruptions.
        self._cycle_image_paths: list[str] = []
        # Per-step wall-clock timings for the CURRENT cycle attempt (reset at
        # the top of every _run_cycle(), same lifetime as _cycle_image_paths
        # above). Populated by _time_step() as each phase runs, logged step-
        # by-step in real time AND as one summary line per cycle exit point
        # (cycle.timing_summary) — added 2026-08-20 (optimize-speed branch)
        # so where cycle time actually goes is visible from the logs alone,
        # not guessed at.
        self._cycle_timings: dict[str, float] = {}
        self._cycle_t0: float = 0.0
        # UNUSED as of 2026-08-21 — every attempt at locking/converging EoA
        # white balance once and skipping it on later cycles (both a naive
        # Continuous-then-forced-Off, and a correctly-polled "Once", tried
        # in both the with-backlight and no-backlight phases) produced bad
        # real-hardware results one way or another. Reverted to plain
        # Continuous every single cycle (see DRAIN_FRAMES in _run_cycle()'s
        # "Frame 2" step) — no locking attempted at all right now. Left
        # defined + CameraDriver.lock_white_balance() left implemented in
        # case this is revisited.
        self._eoa_wb_locked: bool = False

    # ------------------------------------------------------------------
    # Public API — called by UI via bridge (thread-safe)
    # ------------------------------------------------------------------

    def operator_start(self) -> None:
        self._put_event(OperatorStartEvent())

    def operator_stop(self) -> None:
        self._put_event(OperatorStopEvent())

    def operator_abort(self) -> None:
        self._put_event(OperatorAbortEvent())

    def operator_acknowledge(self) -> None:
        """After e-stop reset or protective stop — resume."""
        self._put_event(OperatorAcknowledgeEvent())

    def configure_pre_run(
        self,
        field_code: str,
        plate_ids: dict[int, str],
        starting_wells: dict[int, str | None],
        selected: dict[int, PlateConfig],
    ) -> None:
        """
        Set up everything the pre-run setup screen collects for the NEXT
        run (FR-UI-01): field code, a Plate ID per station, an optional
        starting well per station, and which stations to fill (Excel
        upload + station map taps). Supersedes the older
        configure_stations() (2026-08-27) -- same station-ordering/
        sequencer-reset behavior, plus field_code/plate_ids/starting_wells.

        Builds self._station_order as right-side stations first (ascending
        by number), then left-side stations (ascending) -- per the
        operator's explicit one-way requirement: finish every selected
        right-side station completely before ever switching to the left
        side, never interleaved, never switching back.

        starting_wells lets a station resume mid-plate (FR-RUN-06/07,
        e.g. after a field-code change mid-run) -- PlateConfig.wells_from()
        already does exactly this, slicing the well list to start from the
        given well (inclusive). None (or missing) means start from the
        first well, unchanged from before.

        Only meaningful while IDLE, before START -- like _next_well_index,
        this cursor is a per-run thing; call again to reconfigure between
        runs. Raises RuntimeError if machine_config.yaml has no
        plate_stations section (multi-station mode not configured at all).
        """
        if self._plate_stations_cfg is None:
            raise RuntimeError(
                "configure_pre_run() called but machine_config.yaml has no "
                "plate_stations section -- multi-station mode is not available."
            )
        self._field_code = field_code
        self._plate_ids_by_station = dict(plate_ids)
        self._selected_stations = {
            num: (
                PlateConfig(source_file=plate.source_file,
                            wells=plate.wells_from(starting_wells[num]))
                if starting_wells.get(num) else plate
            )
            for num, plate in selected.items()
        }
        right = sorted(n for n in selected if self._plate_stations_cfg.side_of(n) == "right")
        left = sorted(n for n in selected if self._plate_stations_cfg.side_of(n) == "left")
        self._station_order = right + left
        self._current_station_idx = 0
        self._current_well_idx = 0
        log.info("orchestrator.pre_run_configured", order=self._station_order,
                 field_code=field_code, plate_ids=self._plate_ids_by_station)

    def operator_configure_pre_run(
        self,
        field_code: str,
        plate_ids: dict[int, str],
        starting_wells: dict[int, str | None],
        selected: dict[int, PlateConfig],
    ) -> None:
        self._put_event(OperatorConfigurePreRunEvent(
            field_code=field_code, plate_ids=plate_ids,
            starting_wells=starting_wells, selected=selected,
        ))

    def operator_shutdown(self) -> None:
        """
        Disconnect all hardware cleanly and stop the orchestrator.
        Safe to call from any state. After this, the asyncio loop exits naturally —
        no force-stop needed. Call this before closing the Qt window.
        """
        self._put_event(OperatorShutdownEvent())

    def _put_event(self, event: SystemEvent) -> None:
        """Submit an event from any thread safely."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        else:
            self._queue.put_nowait(event)

    @property
    def state(self) -> State:
        return self._sm.current

    @property
    def light_curtain_tripped(self) -> bool:
        """Live light-curtain state, updated every 100ms by the safety poll.
        Used by the UI to gate the CONTINUE button (FR-UI-05)."""
        return self._light_curtain_tripped

    @property
    def hardware_fault_active(self) -> bool:
        """
        Live "any Flexi fault latched" state, updated every 100ms. Stays
        True through an e-stop's entire hardware reset sequence (button
        released, awaiting reset) and only clears once the reset button has
        actually been pressed too. Used by the UI to gate CONTINUE for
        E_STOP the same way light_curtain_tripped gates it for the light
        curtain (FR-SAF-05).
        """
        return self._hardware_fault_active

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Start all device workers and the main event loop.
        Call this from main.py via asyncio.run().
        """
        self._loop = asyncio.get_running_loop()
        self._running = True
        log.info("orchestrator.starting")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._startup())
            tg.create_task(self._safety_poll_loop())
            tg.create_task(self._plc_poll_loop())
            tg.create_task(self._robot_keepalive_loop())
            tg.create_task(self._event_loop())

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Startup — connect all devices
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        log.info("orchestrator.connecting_devices")
        d = self._drivers
        try:
            # Remote Ethernet restart, unconditionally, before anything else
            # touches the network -- guarantees sort_server.prg is freshly
            # (re)started and motors freshly cycled on every launch,
            # regardless of what state the controller was left in.
            # DELIBERATELY LEAVES THE CONNECTION OPEN on success (2026-08-30
            # real-hardware finding, reversed from an earlier connect-
            # briefly-then-close design) -- see
            # _remote_ethernet_restart_task()'s docstring for the full
            # story: closing it right after Start reliably killed the task
            # within seconds, every time, regardless of network contention
            # from other devices (tested and ruled out separately). Held
            # open for the rest of the run; only shutdown closes it.
            # Best-effort: a failure here is logged but does not abort
            # startup -- the plain d.robot.connect() right after surfaces
            # any real problem through the one existing error-reporting
            # path (this method's own except block below).
            if self._robot_remote_enabled:
                await self._remote_ethernet_restart_task(d)

            # Connect the fast, safety-critical devices first (Flexi, PLC,
            # pump -- none of these do multi-second discovery). Lighting
            # deliberately NOT connected here (2026-09-01, operator
            # observation) -- see the lighting.connect() call further down,
            # right before the real backlight-on. Cameras deliberately NOT
            # connected yet either -- see below.
            await asyncio.gather(
                d.safety.connect(),
                d.plc.connect(),
                d.pump.connect(),
            )
            # Unblocks _safety_poll_loop()/_plc_poll_loop() now that both
            # devices are actually connected -- see _safety_plc_connected's
            # docstring in __init__.
            self._safety_plc_connected.set()
            # Connect robot now and home it IMMEDIATELY, before cameras ever
            # touch the network (2026-08-27 real-hardware finding). GigE
            # camera discovery/connect takes ~6s and is chatty (GVCP
            # broadcast discovery, parameter negotiation) on the same
            # shared USB-Ethernet adapter Flexi/PLC/robot all use -- with
            # cameras connecting concurrently, that traffic was contending
            # with this exact HOME call, occasionally starving RC+'s own
            # link to the controller long enough to trip a real
            # "Communication disconnection detected" (Epson error
            # 1503/1504 -- disconnection while executing a task) and abort
            # the move mid-motion. Robot still never sits idle waiting
            # (the original reason cameras were connected first) -- it
            # gets a command the instant it connects, same as before.
            # Retry with backoff (2026-08-30 real-hardware finding): even
            # with sort_server.prg's own OpenNet retry-on-failure fix
            # (see its server_start: label), the exact moment its socket
            # becomes acceptable to connect to isn't guaranteed to land
            # before this first attempt -- a single unretried connect()
            # here would race it. Cheap to retry a few times; only a real,
            # persistent failure should actually raise.
            last_exc: RuntimeError | None = None
            for attempt in range(3):
                try:
                    await d.robot.connect()
                    last_exc = None
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    if attempt < 2:
                        log.warning("orchestrator.robot_connect_retry",
                                    attempt=attempt + 1, error=str(exc))
                        await asyncio.sleep(2.0)
            if last_exc is not None:
                raise last_exc
            # Motor-only pipette release, before anything else (2026-08-31,
            # operator request): guarantees a specimen can never be
            # silently left in the tip from a crashed/interrupted previous
            # run -- every fresh start begins with a known-empty pipette.
            # No robot arm travel (safe to call before homing) -- best-
            # effort, logged loudly but does not abort startup, since a
            # transient pipette-motor hiccup here shouldn't block cameras/
            # homing/the rest of the system from coming up.
            try:
                await d.robot.reset_pipette()
                log.info("orchestrator.pipette_reset")
            except Exception as exc:
                log.error("orchestrator.pipette_reset_failed", error=str(exc))
            # Send robot to safe park position immediately after connecting.
            # This lifts Z if the arm was left at imaging height by a previous
            # run, then moves to P0 (taught in RC+ Jog & Teach).
            await self._home_right(d)
            log.info("orchestrator.robot_homed")
            # Lighting connects here, not in the earlier fast-device gather
            # (2026-09-01, operator observation: "backlight turns on and
            # off once [before homing], then after robot homes it turns
            # on" -- an extra, visible on/off blip well before the real
            # turn-on). connect() itself does a one-time, patient check
            # that briefly lights the backlight to confirm LOAD:PROFILE
            # genuinely works on this physical unit (see
            # cib/drivers/lighting/driver.py's _verify_backlight_profiles_
            # sync() docstring for why that check has to be a real,
            # isolated on/off, not folded into a normal set_backlight()
            # call) -- moving the connect() call to right here means that
            # necessary blip happens immediately before the real backlight-
            # on below instead of minutes earlier while the robot is still
            # connecting/homing, with nothing touching the light in between.
            await d.lighting.connect()
            # Turn backlight on immediately.
            # set_backlight(True) blocks until all LEDs are at full brightness (0.5s
            # stabilisation built into the driver) — no extra sleep needed here.
            await d.lighting.set_backlight(True)
            log.info("orchestrator.backlight_on")

            # NOW connect cameras -- robot's one guaranteed-solo move is
            # already done, so their ~6s chatty discovery no longer has
            # anything time-critical to contend with.
            await asyncio.gather(
                d.camera_overhead.connect(),
                d.camera_eoa.connect(),
            )
            log.info("orchestrator.cameras_connected")
            # Ring light — skipped until digital output 12 is confirmed wired
            # in RC+ 7.0. Sending RING_ON when output 12 is not configured
            # causes sort_server.prg's On 12 to block ~5s then throw a SPEL+
            # error, triggering OnErr → CloseNet → TCP RST → reconnect loop.
            # Re-enable once output 12 is configured in the RC+ I/O settings.
            # await d.robot.ring_light_on()
            log.info("orchestrator.ring_light_skipped",
                     note="Re-enable once RC+ digital output 12 is configured")

            # Stop camera streams BEFORE enabling keepalive.
            # Both cameras stream at ~980 Mbps on the shared USB-Ethernet adapter.
            # If streams are still running when the keepalive first fires, the
            # STATUS packet is drowned in camera traffic, the robot's ~1s TCP idle
            # timer fires before the packet arrives, and the robot sends RST
            # (WinError 10054). Stopping streams here — before startup_complete is
            # set — guarantees the adapter is clear when the keepalive starts.
            await asyncio.gather(
                d.camera_overhead.stop_streaming(),
                d.camera_eoa.stop_streaming(),
            )
            log.info("orchestrator.camera_streams_stopped_idle")

            # Mark startup complete AFTER streams are stopped so the keepalive
            # loop only fires into a clear network (no camera bandwidth contention).
            self._startup_complete = True
            await self._queue.put(AllDevicesReadyEvent())
            log.info("orchestrator.all_devices_connected")
        except Exception as exc:
            log.error("orchestrator.startup_failed", error=str(exc))
            # Best-effort cleanup: stop streams then disconnect cameras so
            # arena_api releases devices. Without this, camera threads leave
            # devices open and the next startup fails to create_device.
            for cam in (d.camera_overhead, d.camera_eoa):
                try:
                    await cam.stop_streaming()
                except Exception:
                    pass
                try:
                    await cam.disconnect()
                except Exception:
                    pass
            try:
                await d.lighting.disconnect()
            except Exception:
                pass
            await self._queue.put(ConnectionLostEvent(DeviceName.ROBOT, detail=str(exc)))

    # ------------------------------------------------------------------
    # Device poll loops — run forever in background
    # ------------------------------------------------------------------

    async def _safety_poll_loop(self) -> None:
        """Poll Flexi Compact every 100ms. Put safety events on queue."""
        await self._safety_plc_connected.wait()
        while self._running:
            try:
                status = await self._drivers.safety.read_status()
                self._light_curtain_tripped = status["light_curtain_tripped"]
                self._hardware_fault_active = status["any_fault_active"]
                # Debounce: track how long it's been continuously clear, not
                # just whether it's clear on this one poll -- see
                # LIGHT_CURTAIN_STABLE_CLEAR_S's comment for why.
                now = asyncio.get_event_loop().time()
                if self._light_curtain_tripped:
                    self._light_curtain_clear_since = None
                elif self._light_curtain_clear_since is None:
                    self._light_curtain_clear_since = now
                self._light_curtain_stably_clear = (
                    not self._light_curtain_tripped
                    and self._light_curtain_clear_since is not None
                    and (now - self._light_curtain_clear_since) >= LIGHT_CURTAIN_STABLE_CLEAR_S
                )
                event = SafetyStatusEvent(
                    any_fault_active=status["any_fault_active"],
                    light_curtain_tripped=status["light_curtain_tripped"],
                    estop_active=status["estop_active"],
                    estop_button_index=status["estop_button_index"],
                    contactor_closed=status["contactor_closed"],
                    reset_available=status["reset_available"],
                    light_curtain_stably_clear=self._light_curtain_stably_clear,
                )
                await self._queue.put(event)
            except Exception as exc:
                log.error("safety_poll.error", error=str(exc))
                await self._queue.put(
                    ConnectionLostEvent(DeviceName.SAFETY, detail=str(exc))
                )
            await asyncio.sleep(self._safety_poll_interval_s)

    async def _plc_poll_loop(self) -> None:
        """Poll Logic PLC every 100ms for event log entries."""
        await self._safety_plc_connected.wait()
        while self._running:
            try:
                entries = await self._drivers.plc.read_event_log()
                if entries:
                    await self._queue.put(PLCEventLogEntry(entries=entries))
            except Exception as exc:
                log.error("plc_poll.error", error=str(exc))
                await self._queue.put(
                    ConnectionLostEvent(DeviceName.PLC, detail=str(exc))
                )
            await asyncio.sleep(self._plc_poll_interval_s)

    async def _robot_keepalive_loop(self) -> None:
        """
        Send periodic STATUS to prevent sort_server.prg from closing the idle
        TCP connection. sort_server.prg drops the connection after ~5 s of
        inactivity (its Line Input loop times out). This fires every 2.5 s so
        the connection stays alive while the system is in IDLE between cycles.

        Only fires after _startup_complete is set — sort_server.prg can take
        >5 s to re-enter Line Input after HOME motion, which would cause a
        STATUS timeout and leave a stale response in the TCP buffer.

        The driver's keepalive() skips silently if a motion command currently
        holds _cmd_lock, so there is no risk of interleaving with motion.

        Escalates to the same Remote Ethernet recovery _run_cycle() uses
        (2026-08-30 real-hardware finding): the robot can disconnect while
        genuinely IDLE, between cycles, with no active cycle task to catch
        it — before this, that situation was invisible (driver.py's own
        per-tick reconnect fails silently, never raises) and stayed broken
        indefinitely with no FAULT, no banner, nothing the operator could
        see except log spam. is_ready() (a cheap local check, no network
        I/O) is polled after every keepalive() call instead of relying on
        a raised exception. Guarded against ever touching a robot that's
        mid-cycle (that has its own recovery path) or already halted by a
        real safety fault (E_STOP/PROTECTIVE_STOP/FAULT) or already in
        FAULT (existing recovery path there is CONTINUE/ABORT, not this).
        """
        await asyncio.sleep(5.0)  # brief delay so startup can begin
        while self._running:
            await asyncio.sleep(ROBOT_KEEPALIVE_INTERVAL_S)
            await self._robot_keepalive_tick()

    async def _robot_keepalive_tick(self) -> None:
        """One iteration of _robot_keepalive_loop()'s body, factored out so
        tests can drive it directly without waiting on real sleeps."""
        if not self._startup_complete:
            return   # don't touch robot until HOME + ring_light_on are done
        if self._shutting_down:
            return   # shutdown's own disconnect isn't a fault -- see __init__ note
        try:
            await self._drivers.robot.keepalive()
        except Exception as exc:
            log.debug("robot_keepalive.error", error=str(exc))

        if await self._drivers.robot.is_ready():
            if self._keepalive_consecutive_disconnected > 0:
                log.info("orchestrator.robot_keepalive_recovered")
            self._keepalive_consecutive_disconnected = 0
            return

        if self._cycle_active or self._robot_recovery_in_progress or self._sm.current in {
            State.E_STOP, State.PROTECTIVE_STOP, State.FAULT,
        }:
            # Not our disconnect to handle -- either _run_cycle()'s own
            # exception handler already owns this, the robot is halted by
            # a real safety fault (is_ready() reads False for that too)
            # awaiting the normal CONTINUE/ABORT path, or ABORT's own
            # recovery sequence is already in flight (see
            # _robot_recovery_in_progress's __init__ comment -- ABORT can
            # reach IDLE before that sequence finishes, so the state check
            # alone isn't enough to keep this loop from piling on a second,
            # racing reconnect attempt).
            return

        self._keepalive_consecutive_disconnected += 1
        if self._keepalive_consecutive_disconnected == ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD:
            await self._queue.put(SystemErrorEvent(
                message="Robot connection lost while idle — attempting to reconnect."
            ))

        now = asyncio.get_event_loop().time()
        if (
            self._robot_remote_enabled
            and self._keepalive_consecutive_disconnected >= ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD
            and (now - self._keepalive_last_recovery_attempt) >= ROBOT_KEEPALIVE_RECOVERY_COOLDOWN_S
        ):
            self._keepalive_last_recovery_attempt = now
            log.error("orchestrator.robot_keepalive_attempting_remote_recovery")
            if await self._recover_robot_connection(self._drivers):
                self._keepalive_consecutive_disconnected = 0
                log.info("orchestrator.robot_keepalive_recovered_via_remote_ethernet")
                await self._queue.put(SystemErrorEvent(
                    message="Robot reconnected automatically via Remote Ethernet."
                ))
            else:
                log.error("orchestrator.robot_keepalive_recovery_failed")
                self._sm.fault()
                await self._drivers.plc.set_status_light(StatusLight.RED_CHASE)
                await self._queue.put(SystemErrorEvent(
                    message="Robot disconnected while idle and automatic recovery "
                            "failed. Check sort_server.prg is running in RC+ (F5), "
                            "then press ABORT."
                ))

    # ------------------------------------------------------------------
    # Main event loop — state machine + command dispatch
    # ------------------------------------------------------------------

    async def _event_loop(self) -> None:
        """
        Read events from the queue, feed to state machine, dispatch commands.
        This is the single decision-making point for the entire system.
        Exits cleanly when _running is set to False by OperatorShutdownEvent.
        """
        while self._running:
            event = await self._queue.get()

            if not self._running:
                break   # shutdown event already processed, don't handle more

            if self._on_event:
                self._on_event(event)

            await self._handle_event(event)

    async def _handle_event(self, event: SystemEvent) -> None:
        sm = self._sm
        d = self._drivers

        match event:

            # ---- Startup ----
            case AllDevicesReadyEvent():
                sm.all_devices_ready()
                await d.plc.set_status_light(StatusLight.GREEN)
                await d.plc.set_work_light(True)

            # ---- Operator commands ----
            case OperatorStartEvent():
                if sm.is_safe_to_start():
                    sm.start()
                    self._visited_entries.clear()   # fresh run — reset visited set
                    self._overhead_capture_generation = 0
                    await self._ensure_run_open()
                    await d.plc.set_status_light(StatusLight.RUNNING)
                    await d.plc.set_work_light(False)
                    await d.lighting.set_backlight(True)
                    log.info("orchestrator.backlight_on")
                    if not self._cycle_active:
                        asyncio.create_task(self._run_cycle())

            case OperatorConfigurePreRunEvent(
                field_code=field_code, plate_ids=plate_ids,
                starting_wells=starting_wells, selected=selected,
            ):
                if sm.current == State.IDLE:
                    self.configure_pre_run(field_code, plate_ids, starting_wells, selected)
                else:
                    log.warning("orchestrator.configure_pre_run_ignored",
                                state=sm.current.value,
                                note="pre-run setup can only change while IDLE, before START")

            case OperatorStopEvent():
                if sm.is_running():
                    sm.cycle_stop()
                    await d.plc.set_status_light(StatusLight.AMBER)
                    if not self._cycle_active:
                        # No cycle is running right now (e.g. system was waiting for
                        # samples with no bugs in the dish). Nothing will call
                        # _finish_stop() — do it here immediately.
                        await self._finish_stop(d)
                    # else: backlight stays ON — the active cycle will detect
                    # CYCLE_STOPPING at its next exit point and call _finish_stop().

            case OperatorAbortEvent():
                # Guard against illegal transitions the same way every other
                # handler in this match does (is_safe_to_start(), is_running(),
                # etc.) — found via testing: sm.abort() unconditionally raises
                # MachineError from most states. Without this guard, an
                # out-of-order call here doesn't just fail cleanly — it
                # raises out of _handle_event uncaught, which crashes the
                # whole asyncio.TaskGroup in run(): safety polling, PLC
                # polling, and robot keepalive all die with it, silently.
                if sm.current not in {
                    State.RUNNING, State.IDLE, State.CYCLE_STOPPING, State.E_STOP,
                    State.PROTECTIVE_STOP, State.FAULT,
                }:
                    log.warning("orchestrator.abort_ignored_illegal_state",
                                state=sm.current.value,
                                note="ABORT is only valid from RUNNING/IDLE/CYCLE_STOPPING/E_STOP/PROTECTIVE_STOP/FAULT")
                    return

                if sm.current == State.RUNNING:
                    # Graceful abort (2026-08-30, operator request): a
                    # one-click "stop everything" from RUNNING must not be
                    # an immediate hard stop mid-motion — it lets the
                    # in-progress cycle finish completely first (any
                    # already-picked specimen gets dropped normally, same
                    # as a plain CYCLE STOP already guarantees), THEN
                    # closes the run and returns to the pre-run screen.
                    # _finish_stop() does the actual close once the cycle
                    # boundary is reached, gated on this flag.
                    self._abort_after_cycle = True
                    sm.cycle_stop()
                    await d.plc.set_status_light(StatusLight.AMBER)
                    if not self._cycle_active:
                        # No cycle running right now (e.g. waiting for
                        # samples) — nothing will call _finish_stop() on
                        # its own, same reasoning as OperatorStopEvent above.
                        await self._finish_stop(d)
                    return

                if sm.current == State.PROTECTIVE_STOP:
                    # ABORT now actively resolves a still-latched hardware
                    # fault itself, the same way CONTINUE does (2026-09-14
                    # fix, reported live: "why do we get only continue
                    # enabled" -- ABORT used to require _hardware_fault_active
                    # to ALREADY be False before doing anything, which
                    # nothing except CONTINUE's own pulse ever cleared, so
                    # ABORT could never actually become usable on its own).
                    # Mirrors CONTINUE's own PROTECTIVE_STOP branch exactly:
                    # curtain-clear is checked UNCONDITIONALLY (not gated
                    # behind _hardware_fault_active) since that's the one
                    # precondition no software reset can work around.
                    if not self._light_curtain_stably_clear:
                        log.warning(
                            "orchestrator.abort_blocked_hardware_not_reset",
                            state=sm.current.value,
                            light_curtain_stably_clear=self._light_curtain_stably_clear,
                            note="Clear the light curtain before ABORT.",
                        )
                        return
                    if self._hardware_fault_active:
                        await d.safety.send_reset_acknowledge()
                        cleared = await self._wait_for_hardware_fault_clear(timeout_s=2.0)
                        if not cleared:
                            log.warning(
                                "orchestrator.abort_blocked",
                                reason="reset-acknowledge sent but hardware fault did not clear",
                                hardware_fault_active=self._hardware_fault_active,
                            )
                            await self._queue.put(SystemErrorEvent(
                                message="Sent the reset signal but the safety controller "
                                        "didn't clear. Press the physical blue reset "
                                        "button on the Flexi panel, then ABORT again."
                            ))
                            return
                elif sm.current == State.E_STOP:
                    # Mirrors CONTINUE's own E_STOP branch exactly: only
                    # bother with the curtain check + pulse at all if motor
                    # power is still actually cut.
                    if self._hardware_fault_active:
                        if not self._light_curtain_stably_clear:
                            log.warning(
                                "orchestrator.abort_blocked_hardware_not_reset",
                                state=sm.current.value,
                                light_curtain_stably_clear=self._light_curtain_stably_clear,
                                note="Clear the light curtain before ABORT.",
                            )
                            return
                        await d.safety.send_reset_acknowledge()
                        cleared = await self._wait_for_hardware_fault_clear(timeout_s=2.0)
                        if not cleared:
                            log.warning(
                                "orchestrator.abort_blocked",
                                reason="reset-acknowledge sent but hardware fault did not clear",
                                hardware_fault_active=self._hardware_fault_active,
                            )
                            await self._queue.put(SystemErrorEvent(
                                message="Sent the reset signal but the safety controller "
                                        "didn't clear. Press the physical blue reset "
                                        "button on the Flexi panel, then ABORT again."
                            ))
                            return

                # Recovery sequence below (deliver/cleanup -> sm.abort() ->
                # home -> reset pipette) races _robot_keepalive_tick()'s own
                # reconnect attempts once sm.abort() reaches IDLE, since IDLE
                # isn't in that loop's E_STOP/PROTECTIVE_STOP/FAULT exclusion
                # set -- see _robot_recovery_in_progress's __init__ comment.
                # Held for the whole sequence below, not just while sm.current
                # is still nominally E_STOP/PROTECTIVE_STOP.
                self._robot_recovery_in_progress = True
                try:
                    if sm.current in {State.E_STOP, State.PROTECTIVE_STOP}:
                        # The robot was halt()ed when the fault hit — every
                        # command raises ConnectionError until resume() clears
                        # the driver's halted flag (see driver.py
                        # _reconnect_sync). Found 2026-08-30 while adding the
                        # hardware-clear gate above: this path never called
                        # resume() at all, so _deliver_pending_specimen()/
                        # _home_right() below would always fail on real
                        # hardware (the mock driver doesn't enforce the same
                        # guard, which is why existing tests never caught it).
                        # Same call _safe_recovery() makes at its own start.
                        try:
                            d.robot.resume()
                        except Exception as exc:
                            log.error("orchestrator.abort_resume_failed", error=str(exc))
                    # Backlight off as early as possible in this handler
                    # (2026-08-31, operator request — see the same change in
                    # OperatorShutdownEvent's handler): previously this was
                    # after _deliver_pending_specimen()/_cleanup_interrupted_
                    # attempt(), both plain unguarded awaits — either failing
                    # would skip backlight-off entirely. Safe to do before
                    # delivering a pending specimen (that motion doesn't need
                    # backlight; it's for overhead vision, not the drop).
                    try:
                        await d.lighting.set_backlight(False)
                    except Exception as exc:
                        log.warning("orchestrator.abort_backlight_error", error=str(exc))
                    if self._pending_well is not None:
                        # A specimen was aspirated when a safety fault interrupted the
                        # cycle, and the operator chose ABORT instead of CONTINUE.
                        # Deliver it to its intended well before closing the run —
                        # exactly the same single motion CONTINUE would do. There is
                        # no safety reason to treat this differently: the only thing
                        # ABORT changes is that picking does NOT resume afterward.
                        # Discarding or leaving it hanging in the tip would be worse
                        # than finishing this one already-in-flight delivery.
                        await self._deliver_pending_specimen()
                    else:
                        # Nothing was aspirated — ABORT can be reached directly from
                        # E_STOP/PROTECTIVE_STOP/FAULT without ever going through
                        # CONTINUE (_safe_recovery()), so this cleanup has to happen
                        # here too, not just there. See _cleanup_interrupted_attempt.
                        await self._cleanup_interrupted_attempt(reason="operator_abort")
                    sm.abort()
                    try:
                        await self._home_right(d)
                        # Re-home the pipette motor as well (2026-09-10 operator
                        # request) -- after any pending specimen was delivered
                        # above and the arm is parked.
                        await self._reset_pipette_motor(d)
                    except Exception as exc:
                        # 2026-09-17 real-hardware finding: this used to be a
                        # bare await -- an Epson controller that isn't back to
                        # "ready" yet right after an E-STOP (needs a few real
                        # seconds even once the physical button is reset)
                        # raised straight out of this handler, uncaught, and
                        # crashed the whole asyncio.TaskGroup (every poll loop
                        # died with it, confirmed live: "unhandled errors in a
                        # TaskGroup" right after this exact call). Land in
                        # FAULT instead -- the same "recovery failed, needs
                        # attention" outcome _recover_robot_connection's own
                        # callers already use, just reached from here too.
                        log.error("orchestrator.abort_home_failed", error=str(exc))
                        sm.fault()
                        await d.plc.set_status_light(StatusLight.RED_CHASE)
                        await self._queue.put(SystemErrorEvent(
                            message="ABORT could not confirm the robot homed after "
                                    "recovery. Check sort_server.prg is running in "
                                    "RC+ (F5), then press ABORT again."
                        ))
                        await self._close_run(RunStatus.ABORTED)
                        return
                    # GREEN, not AMBER (2026-09-16, operator request): this is
                    # the final resting state once ABORT has fully finished --
                    # same "clean, ready, no error" meaning as AllDevicesReadyEvent's
                    # own GREEN, just reached via a different path. AMBER now
                    # means only "currently stopping" (see the transient AMBER
                    # set right when cycle_stop() fires, above).
                    await d.plc.set_status_light(StatusLight.GREEN)
                    await d.plc.set_work_light(True)
                    await self._close_run(RunStatus.ABORTED)
                    log.info("orchestrator.backlight_off")
                finally:
                    self._robot_recovery_in_progress = False

            case OperatorAcknowledgeEvent():
                if sm.current in {State.PROTECTIVE_STOP}:
                    if not self._light_curtain_stably_clear:
                        # Physically blocked -- clearing the beam is the one
                        # precondition no software reset can work around
                        # (FR-SAF-09: only send the reset when the curtain is
                        # actually clear). This USED TO also require
                        # _hardware_fault_active already False (2026-08-18,
                        # back when send_reset_acknowledge() was a no-op and
                        # only the physical blue button could ever clear it)
                        # -- that's backwards now that the reset pulse itself
                        # is what's supposed to clear _hardware_fault_active
                        # (OQ-23 resolved 2026-09-11): requiring it to already
                        # be False meant this branch could never actually
                        # reach the call below. See the else branch.
                        #
                        # Checks the DEBOUNCED flag, not raw
                        # _light_curtain_tripped (2026-09-11 fix, found live:
                        # the raw reading flickers on this installation even
                        # with nothing in the beam -- CONTINUE kept failing
                        # minutes after the operator had genuinely stepped
                        # clear). See LIGHT_CURTAIN_STABLE_CLEAR_S.
                        log.warning(
                            "orchestrator.continue_blocked",
                            reason="light curtain not stably clear",
                            curtain_tripped=self._light_curtain_tripped,
                            stably_clear=self._light_curtain_stably_clear,
                        )
                    else:
                        # Beam is clear — pulse reset-acknowledge to release
                        # the latch (holding register 256, bit 8 -- confirmed
                        # live on real hardware 2026-09-11, see
                        # cib/drivers/safety/driver.py), then VERIFY it
                        # actually cleared before trusting it -- the pulse
                        # could in principle fail to take (some other
                        # condition still blocking reset), so don't just
                        # assume success because the Modbus write itself
                        # didn't error.
                        await d.safety.send_reset_acknowledge()
                        cleared = await self._wait_for_hardware_fault_clear(timeout_s=2.0)
                        if not cleared:
                            log.warning(
                                "orchestrator.continue_blocked",
                                reason="reset-acknowledge sent but hardware fault did not clear",
                                hardware_fault_active=self._hardware_fault_active,
                            )
                            await self._queue.put(SystemErrorEvent(
                                message="Sent the reset signal but the safety controller "
                                        "didn't clear. Press the physical blue reset "
                                        "button on the Flexi panel, then CONTINUE again."
                            ))
                            return
                        robot_ok = await self._safe_recovery()
                        await self._clear_safety_event()
                        if not robot_ok:
                            # Found 2026-08-18: a fault mid-motion can crash the
                            # SPEL+ task itself (sort_server.prg's error handler
                            # only covers the idle wait, not active motion — see
                            # its header comment). Do NOT declare RUNNING with a
                            # robot that isn't actually connected — that just
                            # spins retrying forever while claiming to be fine.
                            log.error(
                                "orchestrator.recovery_failed_robot_unreachable",
                                note="Robot did not reconnect — restart sort_server.prg "
                                     "in RC+ (F5), then press ABORT to clear this and "
                                     "start a new run.",
                            )
                            sm.fault()
                            await self._queue.put(SystemErrorEvent(
                                message="Robot did not reconnect after the light curtain "
                                        "cleared. Restart sort_server.prg in RC+ (F5), "
                                        "then press ABORT to clear this and start a new run."
                            ))
                            await d.plc.set_status_light(StatusLight.RED_CHASE)
                            return
                        if self._pending_well is not None:
                            # Deliver whatever was already in the tip BEFORE any
                            # new pick is allowed to start.
                            await self._deliver_pending_specimen()
                        # Re-home the pipette motor after recovery (and after
                        # any pending specimen is out) -- 2026-09-10 operator
                        # request, same motor home as startup / CYCLE STOP.
                        await self._reset_pipette_motor(d)
                        if self._state_before_fault in (State.RUNNING, State.CYCLE_STOPPING):
                            # Was actually running — resume, per FR-SAF-08 (no
                            # separate START needed for this fault type).
                            sm.start()
                            await d.plc.set_status_light(StatusLight.RUNNING)
                            await d.plc.set_work_light(False)
                            if not self._cycle_active:
                                asyncio.create_task(self._run_cycle())
                        else:
                            # Found 2026-08-18: the curtain can trip while
                            # nothing is running at all (e.g. between cycles).
                            # Settling straight back to IDLE, not RUNNING — do
                            # not start picking that nobody asked for.
                            sm.hardware_reset_complete()
                            await d.plc.set_status_light(StatusLight.GREEN)
                            await d.plc.set_work_light(True)
                        self._state_before_fault = None
                elif sm.current == State.E_STOP:
                    # E-stop is physically cleared (blue button pressed, contactor
                    # re-closed) — robot motor is back on. Safe to home now.
                    #
                    # 2026-09-11, operator request: the physical Reset Button also
                    # feeds the P-Stop side's own Reset V1 block (same OR gate our
                    # software "Reset Motor" pulse feeds, per the Safety Designer
                    # logic diagram) -- but P-Stop's Reset V1 can only release once
                    # the E-Stop's OWN Reset V1 has ALREADY enabled "Enable System",
                    # which its own AND gate also requires the light curtain to be
                    # clear AT THAT SAME INSTANT. One button press usually clears
                    # both together, but if the curtain wasn't simultaneously clear
                    # (or any other timing race), only the e-stop side clears and
                    # the motor-power side (_hardware_fault_active) stays latched --
                    # previously this branch never attempted the software pulse at
                    # all, so the operator had to notice and press the physical
                    # button a SECOND time just to re-latch the motor side. Try the
                    # same pulse CONTINUE already sends for a pure light-curtain
                    # trip first, so one physical action plus CONTINUE is enough.
                    if self._hardware_fault_active:
                        if not self._light_curtain_stably_clear:
                            # Same physical precondition PROTECTIVE_STOP's
                            # branch checks before ever attempting the pulse
                            # (2026-09-11 fix, found live: the P-Stop side's
                            # own Reset V1 that pulse targets requires the
                            # curtain clear to release) -- don't even try a
                            # pulse that's guaranteed to fail, and don't let
                            # a stray/racing OperatorAcknowledgeEvent bypass
                            # this check just because it didn't come through
                            # the UI's own (now-fixed) button gate. Checks
                            # the DEBOUNCED flag, not raw
                            # _light_curtain_tripped -- see
                            # LIGHT_CURTAIN_STABLE_CLEAR_S.
                            log.warning(
                                "orchestrator.continue_blocked",
                                reason="e-stop cleared but light curtain not stably clear",
                                curtain_tripped=self._light_curtain_tripped,
                                stably_clear=self._light_curtain_stably_clear,
                            )
                            await self._queue.put(SystemErrorEvent(
                                message="E-stop released, but the light curtain is "
                                        "still obstructed. Clear the cell, then "
                                        "CONTINUE again."
                            ))
                            return
                        await d.safety.send_reset_acknowledge()
                        cleared = await self._wait_for_hardware_fault_clear(timeout_s=2.0)
                        if not cleared:
                            log.warning(
                                "orchestrator.continue_blocked",
                                reason="e-stop reset-acknowledge sent but hardware fault "
                                       "did not clear",
                                hardware_fault_active=self._hardware_fault_active,
                            )
                            await self._queue.put(SystemErrorEvent(
                                message="E-stop released, but the safety controller "
                                        "still shows a fault. Confirm the physical blue "
                                        "reset button was pressed and the light curtain "
                                        "is clear, then CONTINUE again."
                            ))
                            return
                    robot_ok = await self._safe_recovery()
                    await self._clear_safety_event()
                    if not robot_ok:
                        log.error(
                            "orchestrator.recovery_failed_robot_unreachable",
                            note="Robot did not reconnect — restart sort_server.prg "
                                 "in RC+ (F5), then press ABORT to clear this and "
                                 "start a new run.",
                        )
                        sm.fault()
                        await self._queue.put(SystemErrorEvent(
                            message="Robot did not reconnect after the e-stop was cleared. "
                                    "Restart sort_server.prg in RC+ (F5), then press ABORT "
                                    "to clear this and start a new run."
                        ))
                        await d.plc.set_status_light(StatusLight.RED_CHASE)
                        return
                    if self._pending_well is not None:
                        # Deliver whatever was already in the tip. Lands back in
                        # IDLE afterward (below) — per FR-SAF-06, e-stop recovery
                        # never auto-resumes RUNNING; a separate START is required.
                        await self._deliver_pending_specimen()
                    # Re-home the pipette motor after recovery (and after any
                    # pending specimen is out) -- 2026-09-10 operator request,
                    # same motor home as startup / CYCLE STOP.
                    await self._reset_pipette_motor(d)
                    sm.hardware_reset_complete()
                    await d.plc.set_status_light(StatusLight.GREEN)
                    await d.plc.set_work_light(True)
                    self._state_before_fault = None

            # ---- Shutdown ----
            case OperatorShutdownEvent():
                log.info("orchestrator.shutdown_started")
                # Set before anything else in this handler, including
                # backlight-off below (2026-09-16, real-hardware finding):
                # _robot_keepalive_tick() checks this to recognize this
                # handler's own upcoming d.robot.disconnect() as intentional
                # rather than a real fault -- see __init__'s note on
                # _shutting_down for the exact race this closes (previously
                # the independent keepalive loop would notice the disconnect,
                # race this handler's own robot_remote sequence attempting
                # its own "recovery," and fault the state machine with a
                # red_chase status light during a completely normal
                # shutdown).
                self._shutting_down = True
                # Wait for an in-progress startup to actually finish before
                # touching any hardware (2026-09-16, real-hardware finding):
                # _startup() and this event handler run as independent
                # concurrent tasks in the same TaskGroup (see run()) -- a
                # slow startup (lighting connect taking a long time) with no
                # progress feedback led an operator to hit SHUTDOWN mid-
                # sequence, and shutdown's own teardown then raced
                # _startup()'s still-running one on the same driver
                # instances (both touching the lighting connection at once),
                # crashing the whole TaskGroup ("Event loop stopped before
                # Future completed"). Startup normally finishes in well
                # under this ceiling; it's just a safety bound so a
                # genuinely stuck startup can't block shutdown forever.
                if not self._startup_complete:
                    log.warning(
                        "orchestrator.shutdown_waiting_for_startup",
                        note="Shutdown requested before startup finished -- "
                             "waiting for it to complete first.",
                    )
                    deadline = asyncio.get_event_loop().time() + 60.0
                    while (
                        not self._startup_complete
                        and asyncio.get_event_loop().time() < deadline
                    ):
                        await asyncio.sleep(0.2)
                    if not self._startup_complete:
                        log.warning(
                            "orchestrator.shutdown_startup_wait_timed_out",
                            note="Startup still not complete after 60s -- "
                                 "proceeding with shutdown anyway.",
                        )
                # Blinking white for the whole shutdown sequence (2026-09-16,
                # operator request) -- visible, distinct "shutting down, not
                # a fault" signal for however long the rest of this handler
                # takes. Best-effort: a failed status light write must never
                # block shutdown itself.
                try:
                    await d.plc.set_status_light(StatusLight.WHITE_FLASH)
                except Exception as exc:
                    log.warning("orchestrator.shutdown_status_light_error", error=str(exc))
                # Backlight off FIRST, before anything else in this handler
                # that could raise (2026-08-31, operator request: "however
                # the program closes... ensure the backlight is turned off
                # no matter what, so the next run always starts with it
                # off"). Previously this was several steps down, guarded by
                # nothing before it -- a failure in _close_run() (a plain,
                # unguarded await) would abort the whole handler and skip
                # backlight-off entirely. Now it can't be starved by
                # anything else in here failing.
                try:
                    await d.lighting.set_backlight(False)
                except Exception as exc:
                    log.warning("orchestrator.shutdown_backlight_error", error=str(exc))
                # Work light off (2026-09-16, real-hardware finding: it was
                # never turned off on shutdown at all -- every other place
                # that touches it turns it ON (AllDevicesReadyEvent, IDLE/
                # AMBER restore paths); OFF only ever happened when a RUN
                # actually started. Shutting down while idle -- the normal
                # case -- left the physical light on indefinitely, since
                # it's a PLC-held output that doesn't reset itself just
                # because this process exits). Same "no matter what else
                # fails" placement as backlight above.
                try:
                    await d.plc.set_work_light(False)
                except Exception as exc:
                    log.warning("orchestrator.shutdown_work_light_error", error=str(exc))
                try:
                    await self._close_run(RunStatus.ABORTED)
                except Exception as exc:
                    log.warning("orchestrator.shutdown_close_run_error", error=str(exc))
                try:
                    await d.robot.ring_light_off()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_ring_light_error", error=str(exc))
                # Send BYE to robot — sort_server.prg does CloseNet + Exit Do cleanly.
                # Without this, Python dropping the socket causes a SPEL+ runtime error
                # which leaves the motor On and trips the Flexi safety system.
                try:
                    await d.robot.disconnect()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_robot_error", error=str(exc))
                # Send DISCONNECT to the lighting controller (backlight
                # itself already turned off above) -- avoids WinError 10054
                # on next startup.
                try:
                    await d.lighting.disconnect()
                except Exception as exc:
                    log.warning("orchestrator.shutdown_lighting_error", error=str(exc))
                # Destroy camera devices — arena_api holds a GVCP control channel
                # open per camera. Without explicit disconnect, the camera stays
                # "in use" and the next startup fails with GC_ERR_TIMEOUT on
                # create_device. Must always disconnect cameras on shutdown.
                for cam in (d.camera_overhead, d.camera_eoa):
                    try:
                        await cam.disconnect()
                    except Exception:
                        pass
                # Cut motor power for sure, via Remote Ethernet — plain BYE
                # above does NOT stop sort_server.prg's task or cut motor
                # power (its BYE handler is CloseNet #201 then loop back to
                # accept a new connection; the `Motor Off` near the end of
                # the file is after an infinite Do...Loop and unreachable —
                # confirmed by reading the file). Connects only for this one
                # sequence, then closes — same as everywhere else this
                # driver is used. Best-effort — shutdown must complete even
                # if this fails.
                if self._robot_remote_enabled:
                    rr = d.robot_remote
                    try:
                        # Reuse the already-open session from startup/
                        # recovery if there is one (2026-08-30 — see
                        # _remote_ethernet_restart_task's docstring) — a
                        # fresh Login while Main is still running kills
                        # it, so never double-connect. Only shutdown ever
                        # closes this connection after a successful start.
                        if not self._robot_remote_connected:
                            await rr.connect()
                            self._robot_remote_connected = True
                        try:
                            await rr.stop()
                            # Real-hardware finding (2026-08-30, isolated
                            # lifecycle test): SetMotorsOff sent immediately
                            # after Stop can fail with "error 20 (Controller
                            # is not ready)" -- same class of real settle
                            # delay as Reset's ~2s error-clear lag, just for
                            # Stop instead. Brief fixed wait rather than a
                            # new poll target -- no documented status flag
                            # for "ready to accept the next command" was
                            # found for this specific case.
                            await asyncio.sleep(1.0)
                            await rr.set_motors_off()
                        finally:
                            await rr.disconnect()
                            self._robot_remote_connected = False
                    except Exception as exc:
                        log.warning("orchestrator.shutdown_robot_remote_error", error=str(exc))
                # Sequence done -- stop blinking white, all off. Best-effort,
                # same as the WHITE_FLASH set at the top of this handler.
                try:
                    await d.plc.set_status_light(StatusLight.OFF)
                except Exception as exc:
                    log.warning("orchestrator.shutdown_status_light_error", error=str(exc))
                # Stop all loops — _event_loop exits after _handle_event returns,
                # poll loops exit on next iteration (within 100ms).
                self._running = False
                log.info("orchestrator.shutdown_complete")

            # ---- Safety status (100ms poll) ----
            case SafetyStatusEvent():
                await self._evaluate_safety(event)

            # ---- Connection loss ----
            case ConnectionLostEvent():
                log.error("connection_lost", device=event.device.value, detail=event.detail)
                sm.fault()
                await d.plc.set_status_light(StatusLight.RED_CHASE)

            # ---- PLC event log ----
            case PLCEventLogEntry():
                for entry in event.entries:
                    log.info("plc_event_log", entry=entry)

    async def _evaluate_safety(self, status: SafetyStatusEvent) -> None:
        """Check safety status and trigger state machine transitions if needed."""
        sm = self._sm
        if not status.any_fault_active:
            return

        if sm.current not in {State.RUNNING, State.CYCLE_STOPPING, State.IDLE}:
            return  # already in a fault state

        # Capture what we were doing before this fault, while sm.current is
        # still the pre-fault state — recovery needs this to know whether to
        # resume RUNNING or just settle back to IDLE (see constructor comment).
        self._state_before_fault = sm.current

        if status.estop_active:
            log.warning("safety.estop_detected", button=status.estop_button_index)
            await self._drivers.robot.halt()
            sm.safety_fault_estop()
            await self._drivers.plc.set_status_light(StatusLight.RED)
            await self._record_safety_event(SafetyEventType.ESTOP)

        elif status.light_curtain_tripped:
            log.warning("safety.light_curtain_tripped")
            await self._drivers.robot.halt()
            sm.safety_fault_protective()
            await self._drivers.plc.set_status_light(StatusLight.RED_FLASH)
            await self._record_safety_event(SafetyEventType.LIGHT_CURTAIN)

    # ------------------------------------------------------------------
    # Operating cycle — called as a background task when RUNNING
    # ------------------------------------------------------------------

    async def _finish_stop(self, d: DriverSet) -> None:
        """
        Called at every cycle exit point when state is CYCLE_STOPPING.
        Completes the stop: turns backlight off, parks robot, transitions to IDLE.

        If self._abort_after_cycle is set (operator pressed ABORT while
        RUNNING, not just STOP -- 2026-08-30), also closes the run once
        this stop completes, so the operator lands back on the pre-run
        setup screen instead of a resumable paused state. The graceful
        part (letting the in-progress cycle finish, delivering any
        already-picked specimen) is already guaranteed by CYCLE_STOPPING
        itself -- this just decides what happens AFTER that finish.
        """
        self._sm.cycle_complete()
        await d.lighting.set_backlight(False)
        await self._home_right(d)
        # Re-home the pipette plunger motor too (2026-09-10 operator
        # request) -- after the arm is parked, same as at startup. A
        # graceful CYCLE STOP already ran the last cycle's DROP, so the
        # plunger is at/near the dispense stop; EX G3 handles that fine
        # (it's why RESET_PIPETTE calls MotorHome, not MotorDrop).
        await self._reset_pipette_motor(d)
        # GREEN, not AMBER (2026-09-16, operator request) -- see the same
        # note in OperatorAbortEvent's handler. _finish_stop() is only ever
        # reached via a deliberate operator STOP or a graceful RUNNING
        # abort, never an error path, so "stopped cleanly, ready again" is
        # always the right meaning here.
        await d.plc.set_status_light(StatusLight.GREEN)
        await d.plc.set_work_light(True)
        log.info("orchestrator.stopped")
        if self._abort_after_cycle:
            self._abort_after_cycle = False
            await self._close_run(RunStatus.ABORTED)
            log.info("orchestrator.abort_after_cycle_complete")

    async def _wait_for_hardware_fault_clear(self, timeout_s: float = 2.0) -> bool:
        """
        Poll self._hardware_fault_active for up to timeout_s, returning True
        as soon as it clears (or immediately if already clear), False on
        timeout. That attribute is written directly by _safety_poll_loop()
        every _safety_poll_interval_s (100-200ms) independent of whether the
        matching SafetyStatusEvent has been processed yet, so polling it
        here reflects the live Flexi state, not stale queue state.

        2026-09-11 (OQ-23 resolved): called right after
        send_reset_acknowledge() actually does something for the first time
        (previously a no-op) -- the Modbus write not erroring doesn't
        guarantee the Flexi's latch actually released (some other condition
        could still be blocking reset), so this verifies the real effect
        instead of assuming success.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if not self._hardware_fault_active:
                return True
            await asyncio.sleep(0.05)
        return not self._hardware_fault_active

    async def _safe_recovery(self) -> bool:
        """
        Called after any safety fault (e-stop or protective stop), once the
        operator has confirmed the hardware is physically clear/reset, and
        BEFORE resuming normal operation.

        What it does:
          0. Calls robot.resume() to clear the driver's halted flag — until
             this is called, the driver refuses to reconnect at all (see
             cib/drivers/robot/driver.py _reconnect_sync), which is exactly
             what we want DURING the fault but must be lifted before any of
             the steps below can talk to the robot again.
          1. Waits for the active cycle task to exit (it will have raised on
             the halted robot and set _cycle_active=False in its finally block).
          2. Stops all camera streams (best-effort — may already be stopped).
          3. Turns ring light off (best-effort — may already be off).
          4. Turns backlight ON (best-effort) — every cycle assumes it starts
             this way; a fault mid-EoA-capture would otherwise leave it off.
          5. Restores EoA camera exposure to normal (best-effort) — a fault
             mid-EoA-capture could otherwise leave the shorter backlight-on
             exposure active for the next cycle's classification frame.
          6. Sends HOME: sort_server.prg moves Z to SAFE (-47.700mm) first,
             then XY. This means the pipette tip retracts before any horizontal
             movement, so it is safe regardless of what phase was interrupted.

        Homing here is always correct even if a specimen is still in the
        pipette — retracting up and going home never drops it. Delivering an
        in-transit specimen to its well is a SEPARATE step the caller runs
        AFTER this returns, once self._pending_well is set — see
        _deliver_pending_specimen() and the OperatorAcknowledgeEvent handler.

        Returns True if the robot actually reconnected and homed, False
        otherwise. Found 2026-08-18: if the fault interrupted the robot
        mid-motion, sort_server.prg's own error handling doesn't cover that
        (only covers the idle Line Input wait — see sort_server.prg header),
        so the SPEL+ task itself can crash, leaving nothing listening on
        port 2000. Homing then fails with a plain connection-refused error.
        The caller MUST check this return value and refuse to resume RUNNING
        if it's False — otherwise the system declares itself "running" while
        the robot is silently disconnected, and just spins retrying forever.
        """
        d = self._drivers
        phase = self._cycle_phase
        log.info("orchestrator.safe_recovery_start", interrupted_phase=phase.name,
                 specimen_pending=self._pending_well is not None)
        # First thing, before any of the (possibly slow) steps below —
        # CONTINUE otherwise visibly does nothing for however long recovery
        # takes (up to ~30s if the Remote Ethernet fallback below fires).
        await self._queue.put(RecoveryStatusEvent(message="Recovering..."))

        # If nothing was aspirated yet, this interrupted attempt never
        # actually became a pick — clean it up so the next cycle starts
        # completely fresh (see _cleanup_interrupted_attempt docstring).
        # Pure DB/filesystem work, independent of robot connectivity, so do
        # it regardless of whether homing below succeeds.
        if self._pending_well is None:
            await self._cleanup_interrupted_attempt(reason=f"safety_fault_during_{phase.name}")

        try:
            d.robot.resume()
        except Exception as exc:
            log.error("orchestrator.safe_recovery_resume_failed", error=str(exc))

        # Wait for the cycle task to finish cleaning up (up to 10s).
        # When _evaluate_safety() calls robot.halt(), the active motion command
        # raises an exception inside _run_cycle(), which exits and sets
        # _cycle_active=False in its finally block. We wait for that here so
        # we don't race it on the robot TCP connection.
        if self._cycle_active:
            log.info("orchestrator.safe_recovery_waiting_for_cycle_exit")
            deadline = asyncio.get_event_loop().time() + 10.0
            while self._cycle_active and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
            if self._cycle_active:
                log.warning("orchestrator.safe_recovery_cycle_still_active",
                            note="Proceeding anyway — robot commands may interleave")

        # Stop streams (best-effort)
        for cam in (d.camera_overhead, d.camera_eoa):
            try:
                await cam.stop_streaming()
            except Exception:
                pass

        # Ring light off (best-effort) — only ever meant to be on briefly
        # during EoA capture, never a resting state.
        try:
            await d.robot.ring_light_off()
        except Exception:
            pass

        # Backlight ON (best-effort) — every cycle's Step 1 assumes it's
        # already on (see _run_cycle() comment: "Backlight already on").
        # If the fault interrupted mid-EoA-capture, where it's briefly OFF
        # for the ring-light image, it would otherwise stay off and starve
        # the next cycle's overhead capture of light.
        try:
            await d.lighting.set_backlight(True)
        except Exception:
            pass

        # EoA exposure restored to normal (best-effort) — mid-EoA-capture,
        # exposure is briefly switched to the shorter backlight-on setting
        # and switched back at the end of that block. If the fault hit in
        # between, the next cycle's classification frame would otherwise be
        # captured at the wrong (too-short) exposure.
        if self._eoa_normal_exposure_us is not None:
            try:
                await d.camera_eoa.set_exposure(self._eoa_normal_exposure_us)
            except Exception:
                pass

        # Home the robot — Z retracts before XY, so always safe
        homed = False
        try:
            await self._home_right(d)
            homed = True
            log.info("orchestrator.safe_recovery_homed", from_phase=phase.name)
        except Exception as exc:
            log.warning(
                "orchestrator.safe_recovery_home_failed_retrying_via_remote_ethernet",
                error=str(exc),
            )
            # A safety fault can interrupt the robot mid-motion, and
            # sort_server.prg's error handling doesn't cover that (only the
            # idle Line Input wait -- see its header) -- the SPEL+ task
            # itself can crash, leaving nothing listening on port 2000, so
            # the plain reconnect above is doomed. Fall back to the same
            # full restart _run_cycle()'s exception handler and the
            # keepalive loop already use: Remote Ethernet Stop/Reset/
            # MotorsOn/Start, then reconnect + rehome (2026-08-31 --
            # previously this path only tried a bare reconnect and gave up
            # here, requiring a manual F5 in RC+ even though the exact
            # mechanism to self-heal already existed elsewhere).
            if self._robot_remote_enabled:
                homed = await self._recover_robot_connection(d)
            if not homed:
                log.error(
                    "orchestrator.safe_recovery_home_failed",
                    error=str(exc),
                    note="Robot did not home — check sort_server.prg is still running in "
                         "RC+ (F5 to restart it), then press CONTINUE again.",
                )

        self._cycle_phase = CyclePhase.IDLE
        log.info("orchestrator.safe_recovery_complete", robot_reconnected=homed)
        return homed

    async def _remote_ethernet_restart_task(self, d: DriverSet) -> bool:
        """
        Best-effort: (re)start sort_server.prg's Main task and cycle motor
        power via Epson RC+ Remote Ethernet. Sequence: connect (Login, if
        not already connected) -> stop (best-effort, clears a possibly-
        wedged prior task) -> reset (clears any latched E-stop/error) ->
        wait_until_error_cleared -> wait_until_ready -> motors on ->
        start(0) -> wait_until_running (confirms the task actually
        launched, not a guessed sleep).

        DELIBERATELY LEAVES THE CONNECTION OPEN on success (2026-08-30,
        reversed from the original connect-briefly-then-close design after
        repeated real-hardware testing): every attempt to close it right
        after Start killed the task within seconds, every time — task
        confirmed healthy (running=True, error_code=0000) right up until
        our own disconnect, dead shortly after, regardless of what else
        was or wasn't happening on the network. Ruled out Logout
        specifically (removed it — same result), ruled out contention
        from other devices connecting afterward (tested Remote Ethernet
        in total isolation for 30+ seconds while connected — completely
        stable; added a settle gap before the next connects — same
        failure anyway). The one constant across every failure was OUR
        OWN disconnect. This mirrors the already-documented "a fresh
        Login while Main is running stops the task" behavior — the
        Remote Ethernet session appears to be this task's actual
        supervisor for as long as it's meant to keep running, the same
        way RC+'s own persistent connection is when a human presses Run
        (which is exactly why manual Run never hits this — that
        connection never closes either).

        self._robot_remote_connected tracks whether the session is
        already open, so a later call (recovery, or shutdown) reuses it
        instead of a second Login — a fresh Login while Main is already
        running kills it, per the finding above, so this must never
        double-connect. Only disconnects (and clears the flag) on
        failure, in the except block below — shutdown is the only place
        that closes it after success (see OperatorShutdownEvent).

        Returns True only if the whole sequence completed without error —
        NOT a guarantee the robot is actually usable again, just that the
        controller accepted the restart. Callers must re-verify by retrying
        the real operation (e.g. d.robot.connect() + _home_right()) afterward.
        Never raises — a failure here must fall through to whatever the
        caller's existing fallback behavior is, not crash it.

        Returns False immediately, doing nothing, if robot_remote_enabled is
        False (the default — see machine_config.yaml's robot_remote section
        for the one-time RC+ setup this requires).
        """
        if not self._robot_remote_enabled:
            return False
        rr = d.robot_remote
        await self._queue.put(RecoveryStatusEvent(
            message="Restarting robot program..."
        ))
        try:
            if not self._robot_remote_connected:
                await rr.connect()
                self._robot_remote_connected = True
            try:
                await rr.stop()
            except Exception as exc:
                log.warning("orchestrator.remote_ethernet_stop_failed", error=str(exc))
            # Settle before Reset -- calling it immediately after Stop can
            # fail with "error 20 (Controller is not ready)" (2026-08-31,
            # real-hardware finding, recurred across multiple live recovery
            # attempts, same class of Stop/Reset timing quirk already
            # documented elsewhere in this project for SetMotorsOff-after-
            # Stop). Retry Reset itself a couple times on that specific
            # error rather than giving up on the very first "not ready"
            # while the controller is still settling.
            await asyncio.sleep(0.5)
            last_reset_exc: Exception | None = None
            for reset_attempt in range(3):
                try:
                    await rr.reset()
                    last_reset_exc = None
                    break
                except Exception as exc:
                    last_reset_exc = exc
                    if "error 20" in str(exc).lower() or "not ready" in str(exc).lower():
                        log.warning(
                            "orchestrator.remote_ethernet_reset_not_ready_retry",
                            attempt=reset_attempt + 1, error=str(exc),
                        )
                        await asyncio.sleep(1.0)
                    else:
                        raise
            if last_reset_exc is not None:
                raise last_reset_exc
            error_cleared = await rr.wait_until_error_cleared(timeout_s=8.0)
            if not error_cleared:
                log.warning("orchestrator.remote_ethernet_error_not_cleared")
                return False
            ready = await rr.wait_until_ready(timeout_s=10.0)
            if not ready:
                log.warning("orchestrator.remote_ethernet_not_ready")
                return False
            await rr.set_motors_on()
            await rr.start(0)
            # Confirm the task actually launched before declaring success
            # — found on real hardware 2026-08-30: a fixed sleep here
            # sometimes wasn't enough (or start() silently didn't take).
            # Poll the Running flag instead of guessing.
            task_running = await rr.wait_until_running(timeout_s=8.0)
            if not task_running:
                log.warning("orchestrator.remote_ethernet_task_not_confirmed_running")
                return False
            log.info("orchestrator.remote_ethernet_restart_sent")
            return True
        except Exception as exc:
            log.warning("orchestrator.remote_ethernet_restart_failed", error=str(exc))
            try:
                await rr.disconnect()
            except Exception:
                pass
            self._robot_remote_connected = False
            return False

    async def _recover_robot_connection(self, d: DriverSet) -> bool:
        """
        Shared mechanics for recovering an unexpectedly-dropped robot
        (port-2000) connection: stop camera streams (best-effort), run
        the same Remote Ethernet restart sequence used at startup, then
        reconnect the normal robot driver and re-home. Used by both
        _run_cycle()'s exception handler (an active-cycle disconnection)
        and _robot_keepalive_loop() (2026-08-30 addition -- found on real
        hardware that a disconnection while sitting IDLE was never being
        recovered at all, since _run_cycle()'s self-heal only ever runs
        during an active cycle; the keepalive loop's own reconnect
        attempts fail silently and never surface anything to the
        operator). Never raises; returns True only if the robot is
        confirmed reconnected and homed.

        Retries the whole restart+reconnect+home sequence up to
        SAFE_RECOVERY_REMOTE_ETHERNET_RETRIES times, SAFE_RECOVERY_RETRY_DELAY_S
        apart (2026-08-31 real-hardware finding): a fault landing in the
        middle of a transient network blip can make the FIRST attempt fail
        for purely timing reasons -- observed live where CONTINUE's
        recovery went straight to FAULT, but a subsequent manual ABORT's
        plain reconnect succeeded moments later with nothing else
        different except elapsed time. All three callers get this
        resilience automatically since they all go through here.

        Bails out immediately if self._running goes False (shutdown
        requested) between attempts -- found on real hardware 2026-08-31:
        this loop's own sleeps let a keepalive-loop-triggered retry run
        well past main.py's ~7s shutdown grace period, which then force-
        stops the event loop mid-sleep and crashes with "Event loop
        stopped before Future completed." No point reconnecting a robot
        we're about to disconnect from anyway.

        Checks d.robot.is_ready() at the top of every attempt, not just
        the first (2026-08-31, real-hardware finding): the robot driver's
        OWN independent keepalive-driven reconnect (see driver.py's
        _keepalive_sync) runs concurrently in the background and can
        succeed entirely on its own between retry attempts here. Observed
        live: attempt 1's Remote Ethernet step failed on "error 20", but
        the robot's own reconnect quietly succeeded moments later --
        then attempt 3's Remote Ethernet step ALSO failed on "error 20"
        and the whole recovery declared failure and went to FAULT, even
        though port 2000 had genuinely been healthy for seconds by then.
        Skip straight to homing whenever it's already ready instead of
        insisting on redoing the entire Remote Ethernet dance regardless.
        """
        for cam in (d.camera_overhead, d.camera_eoa):
            try:
                await cam.stop_streaming()
            except Exception:
                pass

        for attempt in range(SAFE_RECOVERY_REMOTE_ETHERNET_RETRIES):
            if not self._running:
                log.info("orchestrator.recover_robot_connection_abandoned_shutdown")
                return False
            if await d.robot.is_ready():
                log.info("orchestrator.recover_robot_connection_already_healthy")
                try:
                    await self._queue.put(RecoveryStatusEvent(message="Homing robot..."))
                    await self._home_right(d)
                    return True
                except Exception as exc:
                    log.error("orchestrator.recover_robot_connection_home_failed", error=str(exc))
                    # is_ready() was apparently a false positive -- fall
                    # through to the normal Remote Ethernet path below
                    # rather than getting stuck on a bad assumption.
            if await self._remote_ethernet_restart_task(d):
                try:
                    await self._queue.put(RecoveryStatusEvent(message="Reconnecting to robot..."))
                    await d.robot.connect()
                    await self._queue.put(RecoveryStatusEvent(message="Homing robot..."))
                    await self._home_right(d)
                    return True
                except Exception as exc:
                    log.error("orchestrator.recover_robot_connection_home_failed", error=str(exc))
            if attempt < SAFE_RECOVERY_REMOTE_ETHERNET_RETRIES - 1:
                log.warning("orchestrator.recover_robot_connection_retry", attempt=attempt + 1)
                await self._queue.put(RecoveryStatusEvent(
                    message=f"Still reconnecting... (attempt {attempt + 2} "
                            f"of {SAFE_RECOVERY_REMOTE_ETHERNET_RETRIES})"
                ))
                await asyncio.sleep(SAFE_RECOVERY_RETRY_DELAY_S)
        return False

    async def _deliver_pending_specimen(self) -> None:
        """
        A specimen was already confirmed aspirated (self._pending_well is
        set) when a safety fault interrupted the cycle. Called by the
        OperatorAcknowledgeEvent handler, immediately after _safe_recovery()
        (robot already re-homed), and BEFORE anything else is allowed to run
        — this is the hard gate that prevents a new pick from ever starting
        while a specimen is still physically in the tip.

        Delivers the specimen to its originally-intended well and marks that
        pick as successfully completed (DB + CSV) — nothing is lost.
        """
        if self._pending_well is None:
            return
        d = self._drivers
        well_x, well_y, well_u, well_label, pick_id, hand = self._pending_well
        log.warning("orchestrator.delivering_interrupted_specimen",
                    well=well_label, well_xy=f"({well_x:.1f}, {well_y:.1f})", hand=hand)
        try:
            self._cycle_phase = CyclePhase.PUMP_DISPENSING
            if pick_id is not None:
                await self._db_update_pick_status(pick_id, PickStatus.DISPENSING)
            await d.robot.execute_place(well_x, well_y, well_u, hand=hand)
            log.info("orchestrator.interrupted_specimen_delivered", well=well_label)
            if pick_id is not None:
                await self._db_finish_pick(pick_id, PickResult.SUCCESS)
                if self._current_pick is not None:
                    self._current_pick.result = PickResult.SUCCESS
                    self._current_pick.timestamp_end = datetime.now()
                    await self._csv_append(self._current_pick)
            self._cycle_phase = CyclePhase.ROBOT_PARKING
            await self._home_right(d)
        except Exception as exc:
            log.error(
                "orchestrator.interrupted_specimen_delivery_failed",
                well=well_label, error=str(exc),
                note="MANUAL CHECK REQUIRED — specimen may still be in the pipette tip.",
            )
            if pick_id is not None:
                await self._db_finish_pick(
                    pick_id, PickResult.FAIL,
                    error_code="DELIVERY_FAILED",
                    error_message=f"Interrupted-specimen delivery to {well_label} failed: {exc}",
                )
        finally:
            self._pending_well = None
            self._current_pick = None
            self._cycle_phase = CyclePhase.IDLE

    async def _reset_pipette_motor(self, d: DriverSet) -> None:
        """
        Re-home the pipette plunger motor against its dispense hard stop --
        sort_server.prg's RESET_PIPETTE -> MotorHome -> EX G3: low-power
        seek into the stop, hMT Locked-Rotor detect, back off
        HOME_BACKOFF_STEPS (~2mm), redefine that as P=0. Exactly the same
        motor home done once at startup (see _startup()).

        Called at every recovery finalization -- CYCLE STOP, ABORT, and
        safety CONTINUE (e-stop / protective stop) -- so the plunger's zero
        is re-established the drift-proof way after any interruption,
        2026-09-10 operator request. Must run AFTER any pending specimen
        has been delivered (_deliver_pending_specimen does its own DROP):
        EX G3 drives the plunger forward into the hard stop, which would
        expel anything still in the tip.

        Best-effort: logged, never raised, never blocks the recovery it's
        part of -- same contract as the startup call. Goes through
        sort_server.prg, so callers should only reach it once the robot
        program is confirmed alive (past _safe_recovery()'s robot_ok gate).
        """
        try:
            await d.robot.reset_pipette()
            log.info("orchestrator.pipette_reset_on_recovery")
        except Exception as exc:
            log.error("orchestrator.pipette_reset_on_recovery_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Data layer helpers — all DB calls are sync SQLite, run in executor
    # ------------------------------------------------------------------

    async def _db_start_pick(self, pick: Pick) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._db.start_pick, pick)

    async def _db_update_pick_status(self, pick_id: int, status: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.update_pick_status, pick_id, status)

    async def _db_finish_pick(
        self, pick_id: int, result: str,
        error_code: str | None = None, error_message: str | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._db.finish_pick, pick_id, result, datetime.now(), error_code, error_message
        )
        # Single funnel point for every pick completion (success, fail, or
        # interrupted-marked-fail) regardless of call site -- see
        # _push_run_status()'s docstring.
        await self._push_run_status()

    async def _csv_append(self, pick: Pick) -> None:
        if self._storage is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._storage.append_pick, pick)

    async def _record_safety_event(self, event_type: str) -> None:
        if self._current_run is None or self._current_safety_event_id is not None:
            return   # already have an open event, or no run to attach it to
        loop = asyncio.get_running_loop()
        event = SafetyEvent(
            run_id=self._current_run.run_id, event_type=event_type, triggered_at=datetime.now(),
        )
        self._current_safety_event_id = await loop.run_in_executor(
            None, self._db.record_safety_event, event
        )

    async def _clear_safety_event(self) -> None:
        if self._current_safety_event_id is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._db.clear_safety_event, self._current_safety_event_id)
        self._current_safety_event_id = None

    async def _cleanup_interrupted_attempt(self, reason: str) -> None:
        """
        Called on recovery (CONTINUE or ABORT) whenever a cycle was
        interrupted BEFORE a specimen was ever aspirated (self._pending_well
        is None — if it's set, that's the transit case, handled separately
        by _deliver_pending_specimen(), never this method).

        This is the "start completely fresh" behavior for every stage other
        than transit: if a fault hit mid-imaging, mid-moving, or
        mid-classifying, that attempt never actually happened, so:
          1. Any images already saved for it (self._cycle_image_paths) are
             deleted from disk — they're orphaned, nothing will ever
             reference them from a completed record.
          2. If a picks-table row was already created for it (self._current_pick,
             set at Step 8 in _run_cycle), it's snapshotted into
             pick_attempts_log (nothing is silently lost) and then deleted
             from `picks` — it never resulted in a real pick, so it
             shouldn't linger there looking like one.
          3. The next cycle starts fresh from Step 1 (overhead image) —
             already true by construction, nothing extra needed: the next
             _run_cycle() call always starts at Step 1 regardless.
          4. The well-fill sequence is untouched — _next_well_index only
             advances when _get_next_well_coords() is actually called
             (Step 8, right before committing to a pick), so an attempt
             that never got that far never advanced it. The next successful
             pick still goes to the next assigned well, not back to the start.
        """
        import os

        if self._current_pick is not None and self._current_pick.pick_id is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._db.log_interrupted_attempt, self._current_pick, reason
            )
            await loop.run_in_executor(None, self._db.delete_pick, self._current_pick.pick_id)
            log.info("orchestrator.interrupted_pick_deleted",
                     well=self._current_pick.well_position, reason=reason)
            self._current_pick = None

        for path in self._cycle_image_paths:
            try:
                os.remove(path)
                log.debug("orchestrator.interrupted_image_deleted", path=path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.warning("orchestrator.interrupted_image_delete_failed", path=path, error=str(exc))
        self._cycle_image_paths = []

    async def _ensure_run_open(self) -> None:
        """
        Create a new Run row + CSV file if none is currently open. Pressing
        START again after a CYCLE STOP (without ABORT in between) continues
        the SAME run — only ABORT/SHUTDOWN or a fresh start after one of
        those creates a new run id.

        field_code/plate_id are placeholders (see constructor) until the
        real pre-run setup screen exists (PRD FR-SETUP-01).
        """
        if self._current_run is not None:
            return
        loop = asyncio.get_running_loop()
        run = Run(
            run_id=f"run_{datetime.now():%Y%m%d_%H%M%S}",
            field_code=self._field_code,
            started_at=datetime.now(),
        )
        await loop.run_in_executor(None, self._db.create_run, run)
        self._storage = await loop.run_in_executor(
            None, RunStorage, run.run_id, self._runs_base_dir
        )
        self._current_run = run
        self._next_well_index = 0
        log.info("orchestrator.run_opened", run_id=run.run_id, field_code=run.field_code)
        await self._push_run_status()

    async def _close_run(self, status: str) -> None:
        if self._current_run is None:
            return
        loop = asyncio.get_running_loop()
        run_id = self._current_run.run_id
        await loop.run_in_executor(None, self._db.finish_run, run_id, status, self._next_well_index)
        if self._storage is not None:
            await loop.run_in_executor(None, self._storage.close)
        log.info("orchestrator.run_closed", run_id=run_id, status=status)
        self._current_run = None
        self._storage = None
        # run_id=None tells the UI no run is open/resumable -- see
        # RunStatusEvent's docstring for why this drives screen routing.
        await self._queue.put(RunStatusEvent(
            run_id=None, field_code="", started_at=None, pass_count=0, fail_count=0,
        ))

    async def _push_run_status(self) -> None:
        """Push a live pass/fail snapshot for the current run (FR-UI-09).
        Called on run open/close and after every pick finishes. No-op if
        no run is open (nothing for the UI to show)."""
        if self._current_run is None:
            return
        loop = asyncio.get_running_loop()
        counts = await loop.run_in_executor(
            None, self._db.count_picks_by_result, self._current_run.run_id
        )
        await self._queue.put(RunStatusEvent(
            run_id=self._current_run.run_id,
            field_code=self._current_run.field_code,
            started_at=self._current_run.started_at,
            pass_count=counts.get(PickResult.SUCCESS, 0),
            fail_count=counts.get(PickResult.FAIL, 0),
        ))

    def _select_next_target(
        self, all_centroids: list[tuple[float, float]],
    ) -> tuple[float, float] | None:
        """
        Pick the next unvisited candidate from an ALREADY-detected overhead
        centroid list — same bottom-left-first selection as the initial
        pick in _capture_and_select_target(), but without a fresh overhead
        capture. Used both by _capture_and_select_target() for its own
        first selection, and by _run_cycle()'s EoA-reject retry loop
        (2026-08-21, operator's idea: when EoA rejects a candidate, try
        the NEXT one from the SAME overhead capture instead of homing and
        re-running the whole overhead pipeline from scratch).

        Marks the returned candidate visited immediately (same reasoning
        as always: even if EoA rejects it too, we won't re-try this exact
        spot). Returns None once every candidate in the list has been
        tried.
        """
        unvisited = [c for c in all_centroids if not self._is_visited(c)]
        if not unvisited:
            return None
        # Sort: y ascending (leftmost), then x descending (bottommost).
        # First element = bottom-left corner bug.
        unvisited_sorted = sorted(unvisited, key=lambda c: (c[1], -c[0]))
        target_x, target_y = unvisited_sorted[0]
        self._mark_visited(target_x, target_y)
        return (target_x, target_y)

    async def _capture_and_select_target(
        self, is_lookahead: bool = False,
    ) -> tuple[float, float, list[tuple[float, float]]] | None:
        """
        Overhead capture + vision + visited-filter + target selection —
        extracted 2026-08-21 so it can run two ways: normally, at the top
        of a cycle (is_lookahead=False — updates self._cycle_phase and
        writes into self._cycle_timings like every other step), and as a
        background lookahead task fired during the PREVIOUS cycle's drop
        (is_lookahead=True — see the "Lookahead" step at the end of
        _run_cycle()). The lookahead runs concurrently with that drop's
        own robot_drop step, so it must NOT touch self._cycle_phase (would
        clobber the real phase mid-drop, which _safe_recovery() depends on
        if a fault hits during that window) or write into
        self._cycle_timings (would corrupt THAT cycle's own
        cycle.timing_summary with the NEXT cycle's numbers) — see
        _time_step()'s docstring for why the `timings` param exists.

        Returns (x, y, all_centroids) — all_centroids is the FULL detected
        list from this capture (not just unvisited ones), handed back to
        the caller so a later EoA rejection can try the next candidate
        from the same capture via _select_next_target() instead of paying
        for a fresh one. Returns None if the dish has nothing new to try
        right now.
        """
        d = self._drivers
        timings: dict[str, float] = {} if is_lookahead else self._cycle_timings

        # ── Start overhead stream only ────────────────────────────────
        # EoA stream starts later, after the robot has moved to the image
        # site. Running both cameras simultaneously consumes ~980 Mbps on
        # the shared USB-Ethernet adapter, leaving almost no bandwidth for
        # robot TCP traffic. By streaming cameras only when they are
        # actively capturing, each phase has adequate bandwidth:
        #   Overhead capture:  overhead only  (~392 Mbps, Mono8 @ 2 fps)
        #   Robot motion:      no streams     (0 Mbps — robot has full 1G)
        #   EoA capture:       EoA only       (~588 Mbps, BGR8 @ 1 fps)
        # The lookahead case is the one deliberate exception to "no camera
        # streaming during robot motion" — it streams overhead WHILE the
        # robot_drop TCP command is in flight. Watch closely on the first
        # real test for that command's response getting delayed past
        # _motion_timeout by adapter contention (see LOOKAHEAD_TIMEOUT_S
        # and the "Lookahead" step in _run_cycle()).
        if not is_lookahead:
            self._cycle_phase = CyclePhase.OVERHEAD_CAPTURE
        with self._time_step("overhead_capture", timings):
            await d.camera_overhead.start_streaming()
            log.debug("cycle.overhead_stream_started")

            # ── Step 1: Overhead image ────────────────────────────────
            # Backlight already on (turned on at startup / OperatorStartEvent).
            # Drain stale frames before the keeper.
            await d.camera_overhead.capture()  # drain stale frame 1
            await d.camera_overhead.capture()  # drain stale frame 2
            frame = await d.camera_overhead.capture()  # fresh stable frame

            # Archival save of the RAW frame, exactly as the camera produced
            # it — before vision touches it, before any annotation is drawn.
            # Fired in the background (not awaited) same as the annotated
            # save further down, so a slow disk write never holds up the
            # robot's next move. 2026-09-08, operator request.
            if self._unprocessed_overhead_save_dir is not None:
                asyncio.create_task(
                    self._save_unprocessed_overhead_frame_background(frame)
                )

            # Stop overhead stream now — done capturing.
            # Frees ~392 Mbps on the adapter so robot TCP has full
            # bandwidth during move_to_image_site.
            await d.camera_overhead.stop_streaming()
            log.debug("cycle.overhead_stream_stopped")

        if not self._running:
            return None

        # ── Step 2: Overhead vision analysis (thread pool — CPU bound) ─
        if not is_lookahead:
            self._cycle_phase = CyclePhase.OVERHEAD_VISION
        loop = asyncio.get_running_loop()
        with self._time_step("overhead_vision", timings):
            overhead_result = await loop.run_in_executor(
                None, self._run_vision_overhead, frame
            )
        if is_lookahead:
            log.info("cycle.lookahead_capture_timing",
                     **{k: round(v, 3) for k, v in timings.items()})
        # Archival image save happens in the background, NOT awaited here
        # (2026-08-21, optimize-speed pass — operator's own observation:
        # "3-4 secs delay before the robot moves, before the overhead
        # image is saved"). run_overhead_detection() used to save the
        # annotated frame inline — cv2.imwrite of a ~24.5MP PNG is a
        # multi-second blocking disk write, and it was holding up the
        # robot's next move for a file nothing downstream in THIS cycle
        # reads (it's a record for later review, not an input to
        # anything). Detection + centroid math above already completed
        # by the time we get here; the save now just runs alongside
        # everything after it (robot travel, EoA capture, etc.) instead
        # of blocking in front of it.
        if self._overhead_save_images and overhead_result.annotated_frame is not None:
            asyncio.create_task(
                self._save_overhead_frame_background(overhead_result.annotated_frame)
            )

        # ── Select target: bottom-left first ─────────────────────────
        # New capture generation as of THIS overhead frame -- see
        # _is_visited()/MAX_ATTEMPTS_PER_CENTROID: a candidate visited in an
        # earlier generation becomes eligible again here (its one extra
        # chance), while the EoA-reject retry loop below keeps reusing this
        # same generation so it never re-selects a candidate it just
        # rejected out of this exact all_centroids list.
        self._overhead_capture_generation += 1
        all_centroids = overhead_result.centroids_robot_xy
        target = self._select_next_target(all_centroids)
        if target is None:
            if all_centroids:
                log.info("cycle.all_centroids_visited",
                         total=len(all_centroids), lookahead=is_lookahead,
                         note="All detected bugs already attempted this run")
            else:
                log.warning("cycle.no_samples_detected", lookahead=is_lookahead)
            return None
        target_x, target_y = target
        log.info("cycle.target_selected",
                 target_x=target_x, target_y=target_y,
                 total=len(all_centroids), lookahead=is_lookahead)

        # ── VIEW: show overhead with target highlighted ────────────────
        # Skipped for the lookahead case even if self._view is ever turned
        # back on — this runs concurrently with the current cycle's own
        # drop, and popping a debug window from a background task while
        # the main cycle might show its own is more confusing than useful.
        if self._view and not is_lookahead and overhead_result.annotated_frame is not None:
            from cib.vision.pipeline import draw_target_highlight
            # Find pixel centroid for selected target
            try:
                idx = overhead_result.centroids_robot_xy.index((target_x, target_y))
                target_px = overhead_result.centroids_px[idx]
                highlighted = draw_target_highlight(overhead_result.annotated_frame, target_px)
            except (ValueError, IndexError):
                highlighted = overhead_result.annotated_frame
            await loop.run_in_executor(
                None, self._show_image, highlighted,
                f"Overhead — {overhead_result.sample_count} bug(s), targeting ({target_x:.1f}, {target_y:.1f})"
            )

        return (target_x, target_y, all_centroids)

    async def _run_cycle(
        self,
        preselected_target: tuple[float, float, list[tuple[float, float]]] | None = None,
    ) -> None:
        """
        Execute one pick-and-place cycle.
        Matches the operating cycle from PRD Section 12.1.

        Bug selection order: bottom-left first in robot XY space.
          Robot coords: x increases toward bottom of dish, y increases toward right.
          Bottom-left = largest x (bottom), smallest y (left).
          Sort: y ascending first, then x descending — gives bottom-left corner first.

        Visited centroid tracking: centroids snapped to the same 0.1mm grid cell as a
          previously attempted location are skipped. Cleared at run start (OperatorStartEvent).
          Prevents infinite retry if EoA rejects the same debris each cycle.

        preselected_target: (x, y, all_centroids) already found by a
          lookahead capture fired during the PREVIOUS cycle's drop (see
          the "Lookahead" step near the end of this method, 2026-08-21) —
          skips this cycle's own overhead capture/vision entirely and goes
          straight to imaging (x, y), instead of homing first.
          all_centroids is that SAME lookahead capture's full candidate
          list, so if (x, y) gets EoA-rejected, the retry loop below can
          still try the rest of that list instead of going straight home
          (2026-08-22 fix — this used to be a bare (x, y) 2-tuple, which
          crashed with "too many values to unpack" the moment
          _capture_and_select_target()'s return shape grew a third
          element for the retry-loop feature and this call site wasn't
          updated to match — found on the first real hardware run of the
          two features together). None (the normal case) runs the full
          capture as before.
        """
        if not self._sm.is_running():
            return
        if self._cycle_active:
            return   # don't stack concurrent cycles

        if self._stations_exhausted():
            # Every selected station's every well is already filled — no
            # point capturing/picking with nowhere left to drop. Unlike the
            # "no bugs visible right now" case below (which retries after a
            # short sleep), this is permanent for the rest of this run —
            # finish gracefully, same as an operator-pressed STOP, which
            # also returns the robot to right home (Phase 0's hand-aware
            # HOME, already validated).
            log.info("cycle.all_stations_complete")
            if self._sm.is_running():
                self._sm.cycle_stop()
            await self._finish_stop(self._drivers)
            return

        self._cycle_active = True
        self._cycle_phase = CyclePhase.OVERHEAD_CAPTURE
        self._cycle_image_paths = []   # fresh slate — see constructor comment
        self._cycle_timings = {}       # fresh slate — see constructor comment
        self._cycle_t0 = time.monotonic()
        d = self._drivers
        log.info("cycle.start", preselected=preselected_target is not None)

        try:
            # Correct the robot's physical hand BEFORE any overhead capture
            # if it doesn't already match what this cycle's station needs
            # (2026-08-27 real-hardware finding -- see
            # _ensure_physical_hand_for_next_pick's docstring: this is a
            # cheap no-op except right at the start of a run whose first
            # selected station is on the left side, where it closes a real
            # gap -- _startup() always homes Righty regardless of which
            # stations were selected).
            await self._ensure_physical_hand_for_next_pick(d)

            if preselected_target is not None:
                target_x, target_y, all_centroids = preselected_target
                log.info("cycle.using_preselected_target",
                         target_x=target_x, target_y=target_y,
                         candidates=len(all_centroids))
            else:
                target = await self._capture_and_select_target()
                if target is None:
                    self._log_timing_summary("no_bugs")
                    if self._sm.current == State.CYCLE_STOPPING:
                        await self._finish_stop(d)
                    elif self._sm.is_running():
                        await asyncio.sleep(2.0)
                        if self._sm.is_running():
                            asyncio.create_task(self._run_cycle())
                        elif self._sm.current == State.CYCLE_STOPPING:
                            await self._finish_stop(d)
                    return
                target_x, target_y, all_centroids = target

            if not self._running:
                return

            loop = asyncio.get_running_loop()

            # ── Steps 3–6: retry loop ───────────────────────────────────────
            # Image + classify candidates from the SAME overhead capture
            # (all_centroids) until one passes EoA, or the list runs out.
            # Added 2026-08-21 (operator's idea): an EoA rejection used to
            # always go home and let the NEXT cycle pay for a fresh overhead
            # capture — wasteful when the current capture already found
            # other untried candidates sitting right there. Now a rejection
            # just tries the next one from _select_next_target(), reusing
            # the same detection pass, and only goes home once that list is
            # actually exhausted. `break` below (only reached once
            # eoa_result.proceed_with_pick is True) exits to Step 7/8
            # (pick + drop), which stays outside this loop — a rejection
            # inside the loop never falls through to picking.
            eoa_retries = 0
            while True:
                # ── Steps 3–4: Robot moves to image site, backlight stays ON ─
                # Reordered 2026-08-22 (operator's idea): capture the
                # with-backlight (classifier) frame FIRST, run vision on it
                # in the background, and only THEN capture the no-backlight
                # (archival-only) frame concurrently with that vision call —
                # instead of capturing both serially before vision ever
                # starts. Backlight is left on for the move (it's already on
                # from the previous cycle/iteration's cleanup below) since
                # the very first thing we need at the destination is a
                # with-backlight frame — no off/on/settle round trip to pay
                # for it. Stream start is folded into the same gather since
                # it doesn't depend on the robot being there yet.
                # Quadrant U selection: pick once here, reuse for the Step 7
                # correction move below (that move is a tiny EoA-derived delta —
                # always the same quadrant — so recomputing would be redundant
                # and risks flip-flopping right at the quadrant boundary).
                target_u = self._select_quadrant_u(target_y)
                # Same hand for this cycle's pick AND drop -- no crossing
                # happens mid-cycle, so this is computed once here and
                # reused everywhere below. Derived fresh from the
                # sequencer's own position (_current_station_side()), not
                # tracked separately, so it can't drift out of sync with
                # what _get_next_well_coords() later returns for the drop.
                hand = self._current_hand()
                self._cycle_phase = CyclePhase.ROBOT_MOVING
                with self._time_step("robot_to_bug"):
                    await asyncio.gather(
                        d.camera_eoa.start_streaming(),
                        d.robot.move_to_image_site(target_x, target_y, u=target_u, hand=hand),
                    )
                log.debug("cycle.eoa_stream_started")

                # ── Step 4: With-backlight capture — this is the frame Step 5
                # actually classifies on (the no-backlight frame proved too
                # dark for reliable segmentation on this hardware — 2026-08-11
                # diagnosis: Otsu called 70-100% of it "foreground", tripping
                # the coverage guard before real classification even ran).
                # Ring light stays off here (mutually exclusive with backlight,
                # 2026-08-21) — it's already off from the previous iteration's
                # cleanup, nothing to do.
                #
                # DRAIN_FRAMES_WITH_BACKLIGHT / DRAIN_FRAMES_NO_BACKLIGHT:
                # 2026-08-21 testing found >=2 drains needed after an exposure
                # change (see git history — tried 1 and 0 first, both failed:
                # yellow cast at 1, near-black stale-buffer frame at 0).
                # StreamBufferHandlingMode=NewestOnly (camera/driver.py) should
                # now prevent the stale-buffer failure specifically, but the
                # AWB-reconvergence reason still applies.
                # Bumped 2 -> 4 on BOTH captures (2026-08-22, operator request)
                # after the 2026-08-22 reorder made each capture the FIRST
                # frame off a freshly-restarted stream for its own lighting
                # condition (previously the no-backlight capture went first
                # every iteration and incidentally gave AWB one "primer" frame
                # under the current stream session before the with-backlight
                # keeper — now each capture starts AWB from whatever state
                # survived the OTHER capture's very different scene, with only
                # 2 frames to readjust). Real hardware showed this as the
                # with-backlight frame oscillating good/yellow/blown-white/
                # yellow across consecutive attempts, and the no-backlight
                # frame coming back consistently dark and blue-shifted.
                # 4 CONFIRMED CLEAN on real hardware (2026-08-22) — both
                # captures came back correct (no oscillation, no dark/blue
                # cast). Bisected to 3 next — also clean. But each drain
                # frame costs a real ~0.6-0.7s of wall time regardless of
                # drain count (this camera is ~23MP/70MB per BGR8 frame — at
                # ~100MB/s effective USB-Ethernet throughput that's a
                # wire-speed floor per frame, confirmed by matching
                # eoa_capture_with_backlight/no_backlight step timings almost
                # exactly against frame-count x this figure).
                # Tried WITH_BACKLIGHT at 1 (2026-08-22, operator request) to
                # see if the exposure pre-set on the accept path (above) was
                # enough to remove the need for AWB-reconvergence margin on
                # this capture too, not just the exposure-latency frame.
                # REVERTED back to 2 same day — real hardware showed two
                # distinct failures at 1: a blown-white frame (AWB not
                # reconverged — the pre-set trick only helps chained/
                # post-pick cycles, not cold-start or retry-loop-after-reject
                # cycles, which still hit drain=1 with zero head start) and,
                # separately, a motion-blurred frame (the whole frame soft,
                # not just the specimen — the robot arm hadn't finished
                # settling from the arrival move when the keeper was grabbed,
                # plausibly worse now after the Speed 25/SpeedS 120 bump in
                # sort_server.prg). More drain frames cost real wall time,
                # which incidentally gives the arm's vibration time to settle
                # too, so this one number was masking two different root
                # causes. NO_BACKLIGHT untouched (stays at 3) — this change
                # is WITH_BACKLIGHT only, per operator's explicit request.
                DRAIN_FRAMES_WITH_BACKLIGHT = 2
                DRAIN_FRAMES_NO_BACKLIGHT = 3
                self._cycle_phase = CyclePhase.EOA_CAPTURE
                with self._time_step("eoa_capture_with_backlight"):
                    if self._eoa_backlight_on_exposure_us is not None:
                        await d.camera_eoa.set_exposure(self._eoa_backlight_on_exposure_us)
                        log.debug("cycle.eoa_exposure_switched",
                                  exposure_us=self._eoa_backlight_on_exposure_us)
                    for _ in range(DRAIN_FRAMES_WITH_BACKLIGHT):
                        await d.camera_eoa.capture()   # drain — exposure settle + WB readjust
                    eoa_frame_with_backlight = await d.camera_eoa.capture()
                    log.debug("cycle.eoa_captured_with_backlight",
                              drain_frames=DRAIN_FRAMES_WITH_BACKLIGHT)

                # ── Step 5: Fire vision in the background ─────────────────
                # _run_vision_eoa operates on the numpy array already copied
                # out of the camera buffer (GigECameraDriver._capture_sync) —
                # it never touches the camera device, so it's safe to run
                # concurrently with the no-backlight capture below rather
                # than waiting for it to finish first.
                # loop.run_in_executor() already returns an awaitable Future
                # and starts running immediately — it must NOT be wrapped in
                # asyncio.create_task() (that requires an actual coroutine,
                # not a Future; doing it anyway throws "a coroutine was
                # expected, got <Future ...>" — caught by a mock smoke test
                # 2026-08-22 before this ever reached real hardware).
                self._cycle_phase = CyclePhase.EOA_VISION
                vision_t0 = time.monotonic()
                vision_task = loop.run_in_executor(
                    None, self._run_vision_eoa, eoa_frame_with_backlight, target_x, target_y
                )

                # ── Steps 3b–4b: No-backlight capture, concurrent with vision ─
                # Archival-only frame (see Step 5 above) — ring light ON,
                # backlight OFF, full/normal exposure. Wrapped in try/except:
                # if RC+ digital output 12 isn't configured yet, the image
                # will just be dark, not fatal to the cycle.
                with self._time_step("eoa_capture_no_backlight"):
                    if self._eoa_normal_exposure_us is not None:
                        await d.camera_eoa.set_exposure(self._eoa_normal_exposure_us)
                        log.debug("cycle.eoa_exposure_restored",
                                  exposure_us=self._eoa_normal_exposure_us)
                    await d.lighting.set_backlight(False)
                    try:
                        await d.robot.ring_light_on()
                        log.info("cycle.ring_light_on")
                    except Exception as exc:
                        log.warning("cycle.ring_light_on_failed", error=str(exc),
                                    note="EoA image will be dark — configure RC+ output 12 and re-run")
                    for _ in range(DRAIN_FRAMES_NO_BACKLIGHT):
                        await d.camera_eoa.capture()   # same exposure-change + buffer reasons as above
                    eoa_frame_no_backlight = await d.camera_eoa.capture()
                    log.debug("cycle.eoa_captured_no_backlight",
                              drain_frames=DRAIN_FRAMES_NO_BACKLIGHT)
                    # For the run view's camera panel (FR-UI-08) -- see
                    # cib/ui/widgets/camera_feed.py's module docstring for
                    # why this is "last capture," not continuous video.
                    await self._queue.put(EoaFrameCapturedEvent(frame=eoa_frame_no_backlight))

                with self._time_step("eoa_cleanup"):
                    try:
                        await d.robot.ring_light_off()
                        log.info("cycle.ring_light_off")
                    except Exception as exc:
                        log.warning("cycle.ring_light_off_failed", error=str(exc))
                    # Backlight back ON immediately — needed again for
                    # whatever happens next (this loop's next retry
                    # candidate, or the next cycle's overhead capture), and
                    # the sooner it's commanded, the more free settle time
                    # it accumulates before anything actually needs it lit
                    # (robot travel + drain/capture on the next iteration is
                    # comfortably more than BACKLIGHT_SETTLE_S ever was).
                    await d.lighting.set_backlight(True)
                    await d.camera_eoa.stop_streaming()
                    log.debug("cycle.eoa_stream_stopped")

                # ── Step 5b: Join the background vision result ────────────
                # eoa_vision's stored duration is the EXTRA wait incurred
                # after the concurrent no-backlight capture + cleanup already
                # finished — not vision's total runtime since it was fired.
                # Fixed 2026-08-22: originally measured from fire time, which
                # double-counted the overlapped window against
                # eoa_capture_no_backlight/eoa_cleanup in steps_total_s (that
                # sum assumes each named step is serial wall time — true for
                # everything except this one, now that vision runs
                # concurrently with the capture below it). vision_total_s is
                # still logged separately for genuine vision-compute
                # diagnostics; it's just not part of the additive total.
                join_t0 = time.monotonic()
                eoa_result = await vision_task
                vision_extra_wait_s = time.monotonic() - join_t0
                vision_total_s = time.monotonic() - vision_t0
                self._cycle_timings["eoa_vision"] = vision_extra_wait_s
                log.info("cycle.step_timing", step="eoa_vision",
                         duration_s=round(vision_extra_wait_s, 3),
                         vision_total_s=round(vision_total_s, 3))

                # Save — the no-backlight (backlight_off/) archival frame only
                # for a CONFIRMED pick (2026-08-22, operator's call: "we need
                # the images of confirmed pics only for backlight off ...
                # because that has been picked, or else why would we need
                # it"). The annotated with-backlight debug image (tool centre
                # vs body centre markers) is still saved on every attempt,
                # accepted or not — it's the record of WHY something was
                # rejected, separate from the Austin-facing archival frame.
                saved_paths = await loop.run_in_executor(
                    None, self._save_eoa_frames,
                    eoa_frame_no_backlight, eoa_frame_with_backlight,
                    eoa_result,
                )
                if saved_paths is not None:
                    self._cycle_image_paths.extend(p for p in saved_paths if p is not None)

                # ── VIEW: show EoA annotated image ────────────────────────
                if self._view and eoa_result.frame_path:
                    await loop.run_in_executor(
                        None, self._show_image_path, eoa_result.frame_path, "EoA Analysis"
                    )

                # ── Step 6: Go / no-go decision ────────────────────────────
                log.info("cycle.eoa_classification",
                         label=eoa_result.label,
                         proceed=eoa_result.proceed_with_pick,
                         reason=eoa_result.reason,
                         body_center_px=eoa_result.body_center_px or "n/a",
                         offset_from_center_px=eoa_result.offset_from_center_px or "n/a",
                         annotated_image=eoa_result.frame_path or "not saved")
                if eoa_result.proceed_with_pick:
                    # Pre-set exposure back to the backlight-on value now,
                    # while the stream is already stopped (eoa_cleanup ran
                    # above) — not right before the NEXT with-backlight
                    # capture like every other cycle (2026-08-22, operator's
                    # idea). Real pick+drop time elapses before that next
                    # capture, so when its stream restarts, this exposure is
                    # already the active register value with no in-flight
                    # frame from an old value to catch stale — the "GenICam
                    # exposure change takes one frame to apply" latency only
                    # occurs when you change exposure WHILE actively
                    # streaming; there is no in-flight frame to be stale
                    # when the stream hasn't started yet.
                    # This does NOT get AWB reconvergence for free the same
                    # way — Continuous AWB only adapts by processing live
                    # frames, and none flow while the stream is stopped, so
                    # the next with-backlight capture still needs its own
                    # drain frames for that. Only the exposure-latency
                    # portion of the requirement is removed here, not the
                    # (larger, per real hardware testing) AWB portion.
                    if self._eoa_backlight_on_exposure_us is not None:
                        await d.camera_eoa.set_exposure(self._eoa_backlight_on_exposure_us)
                        log.debug("cycle.eoa_exposure_preset_for_next",
                                  exposure_us=self._eoa_backlight_on_exposure_us)
                    break

                eoa_retries += 1
                log.info("cycle.eoa_no_pick",
                         label=eoa_result.label,
                         reason=eoa_result.reason,
                         retries=eoa_retries,
                         note="EoA rejected — trying next candidate from this "
                              "capture instead of homing")
                stopping = self._sm.current == State.CYCLE_STOPPING
                next_candidate = None if stopping else self._select_next_target(all_centroids)
                if next_candidate is not None:
                    target_x, target_y = next_candidate
                    continue   # same overhead capture, no fresh detection needed

                # Exhausted every candidate from this capture (or stopping) —
                # go home, same as the original single-candidate reject path.
                self._cycle_phase = CyclePhase.ROBOT_PARKING
                with self._time_step("robot_park"):
                    await self._home_right(d)
                log.info("orchestrator.robot_returning_backlight_already_on")
                self._log_timing_summary("eoa_reject")
                if stopping:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    asyncio.create_task(self._run_cycle())
                return

            # ── Step 7: Correct robot position using EoA pixel-scale calibration ─
            # body_center_px is where the classifier found the bug body in the EoA
            # image. Compute the robot correction so the body centre lands exactly
            # under the tool tip (image centre), then move there before picking.
            body_center_px = eoa_result.body_center_px
            if body_center_px:
                corrected_x, corrected_y = self._compute_eoa_correction(
                    body_center_px, eoa_frame_with_backlight.shape, target_x, target_y,
                    target_u,
                )
                # Fine correction applied ONLY to the vision-computed PICK target,
                # deliberately not to TLSet 2 or the plate config (2026-08-19,
                # see DEV-15) — those were reverted to their true original values
                # after several rounds of chasing this as a tool-offset problem
                # turned out to be misdiagnosed (the real cause was DEV-14's
                # unrecorded-U EoA calibration). Living here means it can only
                # ever affect PICK, never leak into DROP the way the tool-offset/
                # well-config approach kept doing.
                corrected_x += PICK_TARGET_CORRECTION_X_MM
                corrected_y += PICK_TARGET_CORRECTION_Y_MM
                if hand == "L":
                    # See PICK_LEFT_EXTRA_CORRECTION_X_MM/_Y_MM's module-level
                    # comment -- Lefty-only, added on top of the correction
                    # above, Righty untouched.
                    corrected_x += PICK_LEFT_EXTRA_CORRECTION_X_MM
                    corrected_y += PICK_LEFT_EXTRA_CORRECTION_Y_MM
                if corrected_x != target_x or corrected_y != target_y:
                    log.info("cycle.eoa_correction_computed",
                             overhead_xy=f"({target_x:.3f}, {target_y:.3f})",
                             corrected_xy=f"({corrected_x:.3f}, {corrected_y:.3f})")
                    # No physical move happens here (2026-08-21, optimize-speed
                    # pass — was a separate Tool-1 "eoa_correction_move" +
                    # verification capture, ~5s combined). It's redundant: the
                    # PICK_U call below is an ABSOLUTE move to (corrected_x,
                    # corrected_y) under Tool 2, from wherever the robot
                    # currently sits — traveling there under Tool 1 first only
                    # ever mattered for the verification photo, which is a
                    # diagnostic, not something the pick itself needs. The
                    # math (_compute_eoa_correction above) is unchanged and
                    # still the number that actually drives the pick; only the
                    # now-pointless physical revisit under the wrong tool is
                    # gone. Re-enable the move + verification capture (see git
                    # history) when self._view is True and you want to
                    # visually confirm a correction on-hardware.
                    if self._view:
                        self._cycle_phase = CyclePhase.ROBOT_MOVING
                        with self._time_step("eoa_correction_move"):
                            await d.robot.move_to_image_site(corrected_x, corrected_y, u=target_u, hand=hand)
                        log.info("cycle.eoa_correction_complete",
                                 pick_xy=f"({corrected_x:.3f}, {corrected_y:.3f})")
                        with self._time_step("eoa_verification_capture"):
                            try:
                                # Ring light stays OFF here — backlight is already
                                # on from the with-backlight capture above and
                                # neither this block nor that one turns ring light
                                # back on (2026-08-21: ring light and backlight are
                                # mutually exclusive, operator's explicit call).
                                # This also makes the verification frame accurately
                                # represent the same lighting the classifier saw.
                                await d.camera_eoa.start_streaming()
                                await d.camera_eoa.capture()   # drain stale frame 1
                                await d.camera_eoa.capture()   # drain stale frame 2
                                frame_corrected = await d.camera_eoa.capture()
                                await d.camera_eoa.stop_streaming()
                                corrected_path = await loop.run_in_executor(
                                    None, self._save_corrected_frame, frame_corrected
                                )
                                if corrected_path is not None:
                                    self._cycle_image_paths.append(corrected_path)
                            except Exception as exc:
                                log.warning("cycle.eoa_verification_capture_failed", error=str(exc))

                refined_x, refined_y = corrected_x, corrected_y
            else:
                refined_x, refined_y = target_x, target_y

            # ── Step 8: Pick + drop — travels to the next well in sequence ──
            # Test-scoped well sequencing (2026-08-11) — see
            # prd_updated.md Appendix D for the not-yet-finalized production
            # design (this is NOT that; see _get_next_well_coords() docstring).
            well_x, well_y, well_label, well_u, well_side, side_switch, station_num = self._get_next_well_coords()
            log.info("cycle.picking", refined_x=refined_x, refined_y=refined_y,
                     u=target_u, hand=hand, well=well_label,
                     well_xy=f"({well_x:.1f}, {well_y:.1f})", well_side=well_side)

            # Log this pick attempt now — from here on, a safety fault means
            # a specimen may end up in the pipette, and recovery needs to
            # know exactly which well it was headed to.
            # station_num comes straight from _get_next_well_coords() above —
            # NOT a fresh self._current_station_number() call (2026-09-07 fix,
            # see that method's docstring: the sequencer may already have
            # advanced past this well's station by the time we'd call it here).
            pick = Pick(
                run_id=self._current_run.run_id if self._current_run else "NO_RUN",
                field_code=self._field_code,
                plate_id=self._plate_ids_by_station.get(station_num, self._default_plate_id),
                well_position=well_label,
                pick_status=PickStatus.MOVING_TO_PICK,
                timestamp_start=datetime.now(),
                image_filename=eoa_result.frame_path,
                robot_x=refined_x, robot_y=refined_y,
            )
            pick.pick_id = await self._db_start_pick(pick)
            self._current_pick = pick

            self._cycle_phase = CyclePhase.ROBOT_PICK
            await self._db_update_pick_status(pick.pick_id, PickStatus.ASPIRATING)

            def _record_pending_well() -> None:
                # Called (via execute_pick()'s on_aspirated callback) the
                # instant aspiration is confirmed -- BEFORE the robot has
                # also retracted/traveled back to image height (2026-08-31,
                # real-hardware finding). Previously this only ran after
                # execute_pick() fully returned, which only happens once
                # that whole retract+travel-back also finishes -- a safety
                # fault landing in that later window, after the specimen
                # was genuinely already aspirated, cut the connection
                # before execute_pick() could ever return, so this line
                # never ran even though the specimen physically was
                # already picked up (confirmed live: operator waited for
                # the aspirate to finish, then tripped the light curtain --
                # CONTINUE's recovery found nothing pending to deliver).
                # This is the hard gate: from here until
                # _deliver_pending_specimen() clears it, NOTHING may start
                # a new pick, and any safety fault MUST deliver this
                # specimen on recovery instead of losing it. well_u is
                # ALWAYS this side's HOME U (see _get_next_well_coords'
                # docstring) -- never target_u/the quadrant-pick U, hard
                # safety rule confirmed by the operator.
                self._pending_well = (well_x, well_y, well_u, well_label, pick.pick_id, hand)

            with self._time_step("robot_pick"):
                await d.robot.execute_pick(
                    refined_x, refined_y, u=target_u, hand=hand,
                    on_aspirated=_record_pending_well,
                )
            log.info("cycle.pick_complete", refined_x=refined_x, refined_y=refined_y)

            pick.pick_status = PickStatus.BUG_IN_PIPETTE
            await self._db_update_pick_status(pick.pick_id, PickStatus.BUG_IN_PIPETTE)

            self._cycle_phase = CyclePhase.ROBOT_TO_WELL
            await self._db_update_pick_status(pick.pick_id, PickStatus.DISPENSING)
            self._cycle_phase = CyclePhase.PUMP_DISPENSING
            # Lookahead: fire the NEXT cycle's overhead capture concurrently
            # with THIS drop (2026-08-21, operator's idea — "while the robot
            # reaches to drop the next bug, trigger the next overhead camera
            # capture"). is_lookahead=True keeps it from touching
            # self._cycle_phase / self._cycle_timings (see
            # _capture_and_select_target's docstring). Awaited (with a
            # timeout, not blocking indefinitely) right after the drop
            # finishes — see below.
            # SKIPPED when side_switch is True (2026-08-26): this drop is
            # the last well of this side's stations — after it, the robot
            # must cross hands (go_home_left()) before the next capture,
            # per the operator's explicit "only capture once physically at
            # the new home" requirement. A concurrent lookahead here would
            # be for the wrong side's dish-quadrant U anyway, so it's not
            # just skipped, it would be actively wrong to use.
            lookahead_task = (
                asyncio.create_task(self._capture_and_select_target(is_lookahead=True))
                if not side_switch else None
            )
            with self._time_step("robot_drop"):
                await d.robot.execute_place(well_x, well_y, well_u, hand=hand)
            log.info("cycle.pick_place_complete", refined_x=refined_x, refined_y=refined_y,
                     well=well_label)

            # Delivered successfully — clear the gate and record completion.
            pick.result = PickResult.SUCCESS
            pick.pick_status = PickStatus.DONE
            pick.timestamp_end = datetime.now()
            await self._db_finish_pick(pick.pick_id, PickResult.SUCCESS)
            await self._csv_append(pick)
            self._pending_well = None
            self._current_pick = None

            stopping = self._sm.current == State.CYCLE_STOPPING

            if side_switch:
                # Explicit hand crossing -- always right-to-left, never the
                # reverse (configure_pre_run() only ever orders right-side
                # stations before left-side ones, one-way per run). No
                # lookahead result to consult here (none was fired above).
                self._cycle_phase = CyclePhase.ROBOT_PARKING   # crossing to a home, same phase family as parking
                with self._time_step("robot_cross_to_left"):
                    await self._home_left(d)
                log.info("cycle.side_switched", new_side="left")
                self._log_timing_summary("success")
                if stopping:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    asyncio.create_task(self._run_cycle())
                return

            # ── Lookahead: skip homing if the concurrent capture above
            # already found the next target ──────────────────────────────
            # Bounded wait — in the common case robot_drop takes several
            # seconds and the lookahead capture takes ~1-2s, so this
            # resolves near-instantly; LOOKAHEAD_TIMEOUT_S is just a cap
            # for the uncommon case (slow camera response) so a stuck
            # lookahead can't stall the cycle. Any exception here (camera
            # hiccup, etc.) is treated the same as "no target" — this is a
            # speed optimization, never something that should be able to
            # fault the cycle.
            next_target: tuple[float, float, list[tuple[float, float]]] | None = None
            try:
                next_target = await asyncio.wait_for(lookahead_task, timeout=LOOKAHEAD_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.info("cycle.lookahead_timeout", timeout_s=LOOKAHEAD_TIMEOUT_S)
            except Exception as exc:
                log.warning("cycle.lookahead_failed", error=str(exc))

            if next_target is not None and self._sm.is_running() and not stopping:
                log.info("orchestrator.lookahead_skip_home",
                         target_x=next_target[0], target_y=next_target[1])
                self._log_timing_summary("success")
                asyncio.create_task(self._run_cycle(preselected_target=next_target))
            else:
                self._cycle_phase = CyclePhase.ROBOT_PARKING
                with self._time_step("robot_park"):
                    # Park matching whichever side is currently active --
                    # not always right. When stopping, _finish_stop() below
                    # homes Righty regardless anyway, so it doesn't matter
                    # here; when continuing on the left side with no
                    # lookahead result, parking Righty would just force an
                    # unnecessary swing back left on the very next cycle.
                    # Uses well_side (captured above from THIS cycle's own
                    # _get_next_well_coords() call), NOT a fresh
                    # _current_station_side() call (2026-09-07 fix, same
                    # issue as station_num above) -- on the last well of the
                    # last selected station, the sequencer has already
                    # advanced past the end of self._station_order, so
                    # _current_station_side() would IndexError here too.
                    if stopping or well_side == "right":
                        await self._home_right(d)
                    else:
                        await self._home_left(d)
                log.info("orchestrator.robot_returning_backlight_already_on")
                self._log_timing_summary("success")

                # ── Step 9: Queue next cycle or finish stop ───────────────
                if stopping:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    asyncio.create_task(self._run_cycle())

        except Exception as exc:
            err_str = str(exc)
            # Full traceback, not just str(exc) (2026-09-13 fix, found live:
            # a "cycle.error" with a bare message like "invalid index to
            # scalar variable." gives no way to tell which of dozens of
            # numpy/cv2 calls in the vision pipeline actually raised it).
            tb_str = traceback.format_exc()
            in_safety_stop = self._sm.current in {State.E_STOP, State.PROTECTIVE_STOP}

            # A pick was in flight when this failed. If a specimen was
            # already aspirated (self._pending_well set), leave it alone —
            # it's not lost, recovery will deliver it. Otherwise, this pick
            # never got the specimen — safe to mark it failed and retry.
            if self._current_pick is not None and self._pending_well is None and self._current_pick.pick_id is not None:
                await self._db_finish_pick(
                    self._current_pick.pick_id, PickResult.FAIL,
                    error_code="INTERRUPTED", error_message=err_str[:250],
                )
                self._current_pick = None

            # Camera start_stream failures on USB-Ethernet adapters are
            # transient — don't FAULT, just retry the cycle after a delay.
            if "start_stream" in err_str or "GC_ERR_TIMEOUT" in err_str or "get_buffer" in err_str.lower():
                log.warning("cycle.camera_error_retrying", error=err_str[:120])
                if self._sm.current == State.CYCLE_STOPPING:
                    await self._finish_stop(d)
                elif self._sm.is_running():
                    await asyncio.sleep(3.0)
                    if self._sm.is_running():
                        asyncio.create_task(self._run_cycle())
            elif in_safety_stop:
                # _evaluate_safety() already transitioned us to E_STOP/
                # PROTECTIVE_STOP concurrently — this exception is just the
                # in-flight robot command reacting to halt(). Do NOT also
                # call sm.fault() here: the state machine only allows one
                # active fault state, and calling fault() would silently
                # bounce us from E_STOP/PROTECTIVE_STOP into FAULT, which
                # needs a different (harder) recovery path than what actually
                # happened. Recovery is already correctly gated on the real
                # state — nothing else to do here.
                log.info("cycle.exited_on_safety_fault", state=self._sm.current.value)
            else:
                # Not a safety trip (in_safety_stop already handled that
                # above) — likely an unexplained robot disconnection. Try to
                # self-heal via Remote Ethernet (Stop/Reset/SetMotorsOn/
                # Start, then reconnect+rehome) before dropping into FAULT
                # and waiting for a human — same restart sequence used at
                # startup. Gate on the error actually looking robot-related
                # (see ROBOT_ERROR_MARKERS) so an unrelated failure (e.g. a
                # camera error not already caught above) can never trigger
                # Stop() against a robot that might be perfectly healthy.
                looks_robot_related = self._robot_remote_enabled and any(
                    marker.lower() in err_str.lower() for marker in ROBOT_ERROR_MARKERS
                )
                recovered = False
                if looks_robot_related:
                    log.error("cycle.error_attempting_remote_recovery", error=err_str[:200], traceback=tb_str)
                    recovered = await self._recover_robot_connection(d)
                if recovered:
                    if self._pending_well is not None:
                        # Deliver whatever was already in the tip before any
                        # new pick is allowed to start.
                        await self._deliver_pending_specimen()
                    # Re-home the pipette motor after the self-heal, same as
                    # the operator-driven recovery paths (2026-09-10).
                    await self._reset_pipette_motor(d)
                    # Deliberately does NOT auto-resume RUNNING — same
                    # philosophy as e-stop/light-curtain recovery: an
                    # unattended, unexplained fault landing back in IDLE
                    # still requires an explicit operator START before
                    # picking resumes.
                    self._sm.fault()
                    self._sm.fault_cleared()
                    try:
                        await d.lighting.set_backlight(True)
                    except Exception:
                        pass
                    # GREEN, not AMBER (2026-09-16) -- same "clean, ready,
                    # no error" resting state as _finish_stop()/ABORT's own
                    # GREEN, reached here via a self-healed reconnect
                    # instead of an operator action.
                    await d.plc.set_status_light(StatusLight.GREEN)
                    await d.plc.set_work_light(True)
                    log.info(
                        "cycle.self_healed_after_remote_restart",
                        note="Robot reconnected via Remote Ethernet after an unexpected "
                             "disconnection — landed in IDLE; operator must press START "
                             "again to resume picking.",
                    )
                    await self._queue.put(SystemErrorEvent(
                        message=f"Robot disconnected ({err_str[:150]}) but reconnected on its "
                                f"own via a Remote Ethernet restart. Press START to resume."
                    ))
                else:
                    log.error("cycle.error", error=str(exc), traceback=tb_str)
                    self._sm.fault()
                    await self._queue.put(SystemErrorEvent(message=f"Cycle error: {err_str[:200]}"))
                    await d.plc.set_status_light(StatusLight.RED_CHASE)
                    try:
                        await d.lighting.set_backlight(False)
                    except Exception:
                        pass
        finally:
            self._cycle_active = False
            self._cycle_phase = CyclePhase.IDLE
            # Stop streams so the adapter is free for robot TCP keepalive.
            for cam in (d.camera_overhead, d.camera_eoa):
                try:
                    await cam.stop_streaming()
                except Exception:
                    pass
            # Best-effort ring light off — in case cycle aborted mid-EoA.
            try:
                await d.robot.ring_light_off()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Centroid selection helpers
    # ------------------------------------------------------------------

    def _find_visited_entry(self, centroid: tuple[float, float]) -> dict | None:
        """Entry within centroid_snap_mm of `centroid`, or None (Euclidean distance check)."""
        cx, cy = centroid
        r2 = self._centroid_snap_mm ** 2
        for entry in self._visited_entries:
            if (cx - entry["x"]) ** 2 + (cy - entry["y"]) ** 2 <= r2:
                return entry
        return None

    def _is_visited(self, centroid: tuple[float, float]) -> bool:
        """
        Return True if `centroid` must be skipped by _select_next_target():
        either it's permanently excluded (MAX_ATTEMPTS_PER_CENTROID real
        attempts already used across separate overhead captures), or it was
        already selected during THIS SAME overhead capture (must never be
        picked twice out of one all_centroids list, regardless of its
        attempt count — that's what keeps the EoA-reject retry loop from
        immediately re-selecting the candidate it just rejected).
        """
        entry = self._find_visited_entry(centroid)
        if entry is None:
            return False
        if entry["count"] >= MAX_ATTEMPTS_PER_CENTROID:
            return True
        return entry["generation"] == self._overhead_capture_generation

    def _mark_visited(self, x: float, y: float) -> None:
        """Record one real pick attempt at this centroid for this run."""
        entry = self._find_visited_entry((x, y))
        if entry is None:
            entry = {"x": float(x), "y": float(y), "count": 0, "generation": -1}
            self._visited_entries.append(entry)
        entry["count"] += 1
        entry["generation"] = self._overhead_capture_generation
        log.debug("cycle.centroid_marked_visited", x=x, y=y,
                  attempts=entry["count"],
                  permanently_excluded=entry["count"] >= MAX_ATTEMPTS_PER_CENTROID,
                  total_tracked=len(self._visited_entries))

    def _run_vision_overhead(self, frame: Any) -> VisionOverheadResultEvent:
        """Runs in thread pool — import here to keep vision isolated.

        save_dir=None always here (2026-08-21) — the disk save is no longer
        inline; see the background-task comment in _run_cycle() Step 2 and
        _save_overhead_frame_background() below. Stage 5 in
        run_overhead_detection() still builds the annotated frame
        regardless of save_dir (only the disk write itself is skipped), so
        overhead_result.annotated_frame is still populated for the caller
        to save in the background.
        """
        from cib.vision.pipeline import run_overhead_detection
        result = run_overhead_detection(
            frame,
            save_dir=None,
            save_format=self._overhead_save_format,
        )
        if self._force_sample_detection and not result.centroids_robot_xy:
            # Dev flag: inject a fake centroid at image centre so the pick-place
            # cycle keeps running even with an empty dish.
            # Remove FORCE_SAMPLE_DETECTION from main.py when real detection is ready.
            log.debug("vision.force_sample_injected",
                      note="FORCE_SAMPLE_DETECTION=True in main.py")
            import dataclasses
            result = dataclasses.replace(
                result,
                centroids_robot_xy=[(100.0, 100.0)],
                sample_count=1,
            )
        return result

    async def _save_unprocessed_overhead_frame_background(self, frame: Any) -> None:
        """
        Write the RAW overhead frame to disk (no vision/annotation applied
        yet), off the thread pool. Fired via asyncio.create_task() right
        after the frame is captured in _capture_and_select_target() and
        deliberately NOT awaited there, same reasoning as
        _save_overhead_frame_background() below (a slow disk write must
        never hold up the robot's next move). Reuses _save_annotated_image()
        purely as a "write this array to save_dir with a timestamped
        filename" helper -- it doesn't require the image to actually be
        annotated, and pointing it at self._unprocessed_overhead_save_dir
        (a different folder from self._overhead_save_dir) keeps these
        completely separate from the annotated copies. 2026-09-08, operator
        request. Safe to run concurrently with the vision pass on the same
        `frame` array -- run_overhead_detection() never mutates its input,
        every stage (_to_gray, threshold, annotate, ...) produces a new
        array, so this is a read-only race with another read-only reader.
        """
        from datetime import datetime
        from cib.vision.pipeline import _save_annotated_image
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None, _save_annotated_image,
                frame, self._unprocessed_overhead_save_dir,
                self._overhead_save_format, datetime.now(),
            )
            log.debug("cycle.unprocessed_overhead_frame_saved_background", path=path)
        except Exception as exc:
            log.warning("cycle.unprocessed_overhead_frame_save_background_failed", error=str(exc))

    async def _save_overhead_frame_background(self, annotated_frame: Any) -> None:
        """
        Write the already-annotated overhead frame to disk, off the
        thread pool. Fired via asyncio.create_task() from _run_cycle()
        Step 2 and deliberately NOT awaited there, so a slow disk write
        never holds up the robot's next move (see the comment at that call
        site). Must be an actual coroutine (not just run_in_executor's
        Future) — asyncio.create_task() requires one. Errors are logged,
        not raised — an archival save failing shouldn't fault the cycle,
        and by the time this runs the cycle has already moved on regardless.
        """
        from datetime import datetime
        from cib.vision.pipeline import _save_annotated_image
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None, _save_annotated_image,
                annotated_frame, self._overhead_save_dir,
                self._overhead_save_format, datetime.now(),
            )
            log.debug("cycle.overhead_frame_saved_background", path=path)
        except Exception as exc:
            log.warning("cycle.overhead_frame_save_background_failed", error=str(exc))

    def _run_vision_eoa(self, frame: Any, robot_x: float, robot_y: float) -> VisionEoAResultEvent:
        from cib.vision.eoa import run_eoa_analysis
        return run_eoa_analysis(
            frame,
            save_dir=self._eoa_save_dir,
            robot_xy=(robot_x, robot_y),
        )

    def _save_eoa_frames(
        self,
        frame_no_backlight,
        frame_with_backlight,
        eoa_result: VisionEoAResultEvent,
    ) -> tuple[str | None, str] | None:
        """
        Save EoA frames:

        backlight_off/  — raw no_backlight frame (ring light only — archival record
                          only; too dark for reliable classification on this hardware,
                          see the Step 5 capture comment in _run_cycle())
        backlight_on/   — with_backlight frame (used by classifier) saved RAW,
                          no overlay — 2026-09-07, operator request: this is
                          the same image the Halcon labeling dataset
                          (eoa_labels.csv below) points its pixel_x/pixel_y
                          coordinates into, and a baked-in crosshair/dot/
                          line drawn right at image centre (where the bug
                          usually is) contaminated exactly the pixels a
                          dataset like that needs to stay clean. Previously
                          this saved an annotated copy (green + = tool tip,
                          cyan dot = body centre, orange line = offset) for
                          visual QA -- that overlay is gone; use
                          hardware_tests/verify_eoa_labels.py instead, which
                          draws the same body-centre crosshair on demand
                          from eoa_labels.csv without ever touching the
                          saved file.

        The backlight_off/ archival frame is only written for a CONFIRMED
        pick (2026-08-22, operator's call: "we need the images of confirmed
        pics only for backlight off ... because that has been picked, or
        else why would we need it") — driven by eoa_result.proceed_with_pick,
        not a separate flag. The with_backlight image is always saved
        regardless — it's the record of WHY a candidate was accepted or
        rejected, same as before.

        Also appends one row per with-backlight image to
        backlight_on/eoa_labels.csv (2026-09-01, operator request — building
        a labeled dataset to test against Halcon): image filename,
        classification label, the pixel (x, y) of the bug/specimen from the
        image's own top-left origin (eoa_result.body_center_px — NOT robot
        coordinates), and the robot mm coordinates the pick would use
        (eoa_result.refined_x/y, already computed via EoA pixel-scale
        calibration) for cross-reference. Written for every attempt, accepted
        or rejected, same reasoning as the with_backlight image itself —
        a rejected/no-specimen row is still a useful (negative) label.

        Returns (path_no_backlight, path_with_backlight) as strings — the
        first is None when the pick wasn't confirmed — or None entirely
        if saving is disabled. Callers track non-None paths to delete them
        if this cycle attempt ends up interrupted before ever aspirating
        a specimen.
        """
        import csv
        import cv2
        from datetime import datetime

        if self._eoa_save_dir is None:
            return None

        body_center_px = eoa_result.body_center_px or None
        save_no_backlight = eoa_result.proceed_with_pick

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

        dir_with_bl = self._eoa_save_dir / "backlight_on"
        dir_with_bl.mkdir(parents=True, exist_ok=True)

        # No-backlight: save raw (classifier already saves its own annotated
        # copy) — only for a confirmed pick, see save_no_backlight above.
        path_no_bl: str | None = None
        if save_no_backlight:
            dir_no_bl = self._eoa_save_dir / "backlight_off"
            dir_no_bl.mkdir(parents=True, exist_ok=True)
            path_no_bl = str(dir_no_bl / f"eoa_no_backlight_{ts}.jpg")
            cv2.imwrite(path_no_bl, frame_no_backlight)

        # With-backlight: save RAW, no overlay (2026-09-07 — see docstring:
        # this is the same file eoa_labels.csv's pixel_x/pixel_y point into,
        # and a baked-in crosshair/dot/line at image centre contaminated
        # exactly the pixels a Halcon-comparison dataset needs clean).

        path_with_bl = dir_with_bl / f"eoa_with_backlight_{ts}.jpg"
        cv2.imwrite(str(path_with_bl), frame_with_backlight)

        # Halcon test-dataset ledger — one row per with-backlight image,
        # every attempt (accepted or rejected). Kept as a flat CSV (not a
        # per-image sidecar) so the whole run's labels import as one table.
        labels_csv = dir_with_bl / "eoa_labels.csv"
        is_new = not labels_csv.exists()
        with open(labels_csv, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow([
                    "timestamp", "image_filename", "label",
                    "pixel_x", "pixel_y", "robot_x_mm", "robot_y_mm",
                    "proceed_with_pick", "reason",
                ])
            writer.writerow([
                ts,
                path_with_bl.name,
                eoa_result.label,
                body_center_px[0] if body_center_px else "",
                body_center_px[1] if body_center_px else "",
                eoa_result.refined_x if eoa_result.refined_x is not None else "",
                eoa_result.refined_y if eoa_result.refined_y is not None else "",
                eoa_result.proceed_with_pick,
                eoa_result.reason,
            ])

        log.info("cycle.eoa_frames_saved",
                 no_backlight=path_no_bl or "skipped (not a confirmed pick)",
                 with_backlight=str(path_with_bl),
                 body_center_px=body_center_px or "n/a",
                 labels_csv=str(labels_csv))
        return (path_no_bl, str(path_with_bl))

    def _save_corrected_frame(self, frame) -> str | None:
        """
        Save a post-correction EoA frame (backlight ON) with only a green +
        crosshair at image centre. Bug should sit on the + if correction worked.
        Saved to backlight_on/eoa_corrected_{ts}.jpg alongside the pre-correction
        frame so you can compare before vs. after side-by-side.

        Returns the saved path (str), or None if saving is disabled — see
        _save_eoa_frames() docstring for why callers track this.
        """
        import cv2
        from datetime import datetime

        if self._eoa_save_dir is None:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dir_with_bl = self._eoa_save_dir / "backlight_on"
        dir_with_bl.mkdir(parents=True, exist_ok=True)

        h, w = frame.shape[:2]
        cx, cy = w // 2, h // 2
        vis = frame.copy()
        arm = max(200, w // 14)
        lw  = max(4, w // 800)

        # Green + at image centre
        cv2.line(vis, (cx - arm, cy), (cx + arm, cy), (0, 255, 0), lw)
        cv2.line(vis, (cx, cy - arm), (cx, cy + arm), (0, 255, 0), lw)
        cv2.circle(vis, (cx, cy), arm // 4, (0, 255, 0), lw)
        cv2.circle(vis, (cx, cy), lw * 2,   (0, 255, 0), -1)
        cv2.putText(vis, "AFTER CORRECTION",
                    (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    max(1.5, w / 1800),
                    (0, 255, 0), lw)

        path = dir_with_bl / f"eoa_corrected_{ts}.jpg"
        cv2.imwrite(str(path), vis)
        log.info("cycle.eoa_corrected_frame_saved", path=str(path))
        return str(path)

    # ------------------------------------------------------------------
    # Cycle timing (2026-08-20, optimize-speed branch)
    # ------------------------------------------------------------------

    @contextmanager
    def _time_step(self, name: str, timings: dict[str, float] | None = None):
        """
        Times one cycle phase and logs it immediately as cycle.step_timing,
        plus records it into `timings` (self._cycle_timings by default) for
        the end-of-cycle cycle.timing_summary line. Wrap every phase worth
        optimizing — camera captures, vision analysis, robot moves — so
        where cycle time actually goes is visible straight from the logs,
        not guessed at.

        timings: pass a throwaway dict instead of the default when timing
        something that ISN'T the current cycle's own progress — e.g. the
        lookahead capture in _capture_and_select_target(is_lookahead=True)
        runs concurrently with THIS cycle's drop, and must not write into
        self._cycle_timings (that dict is about to be read by THIS cycle's
        own cycle.timing_summary — writing into it from a background task
        would corrupt that summary with the next cycle's numbers).

        A plain (non-async) context manager is enough here: the `with`
        block's body still runs its `await`s normally, this just measures
        wall-clock time around it.
        """
        t0 = time.monotonic()
        try:
            yield
        finally:
            dt = time.monotonic() - t0
            (timings if timings is not None else self._cycle_timings)[name] = dt
            log.info("cycle.step_timing", step=name, duration_s=round(dt, 3))

    def _log_timing_summary(self, outcome: str) -> None:
        """
        One combined line with every step timed so far this cycle attempt,
        plus two totals — call at every _run_cycle() exit point (success,
        EoA reject, no bugs detected). outcome documents which exit this
        was (e.g. "success", "eoa_reject", "no_bugs") since not every exit
        reaches every step.

        steps_total_s = sum of the named/wrapped steps only.
        wall_total_s  = true wall-clock time since cycle start (_cycle_t0).
        The gap between them is everything NOT individually timed yet
        (DB writes, target selection, sleeps, Python overhead) — worth
        wrapping more granularly later if that gap turns out to be large.
        """
        steps_total = sum(self._cycle_timings.values())
        wall_total = time.monotonic() - self._cycle_t0
        log.info("cycle.timing_summary", outcome=outcome,
                 steps_total_s=round(steps_total, 3), wall_total_s=round(wall_total, 3),
                 **{k: round(v, 3) for k, v in self._cycle_timings.items()})

    def _current_station_side(self) -> str:
        """
        "right" or "left" -- the side of whichever station the sequencer is
        currently positioned at (peek only, does not advance anything).

        This is the single source of truth for which hand THIS cycle's
        pick+drop must use, derived fresh every call from the sequencer's
        own position rather than tracked as separate mutable state — it
        can never drift out of sync with what _get_next_well_coords() is
        about to return (e.g. after a fault-recovery re-home, which always
        parks Righty via move_to_safe_park() regardless of which side the
        run's sequencer is mid-way through — see _deliver_pending_specimen).

        Legacy fallback (multi-station not configured): always "right",
        matching the original Righty-only cycle.
        """
        if not self._station_order:
            return "right"
        station_num = self._station_order[self._current_station_idx]
        return self._plate_stations_cfg.side_of(station_num)

    def _current_station_number(self) -> int | None:
        """
        The station number the sequencer is currently positioned at (peek
        only, does not advance anything), or None if multi-station mode
        isn't configured.

        CAUTION (2026-09-07): self._current_station_idx can legitimately
        equal len(self._station_order) right after _get_next_well_coords()
        returns the LAST well of the LAST selected station -- calling this
        method in that window raises IndexError ("list index out of
        range"), and calling it anywhere between two _get_next_well_coords()
        calls that crossed a station boundary silently returns the NEXT
        station, not the one the most recent well actually came from. This
        is exactly the real-hardware crash fixed this same day -- see
        _get_next_well_coords()'s docstring. Prefer using the station_num
        it already returns instead of calling this method after the fact.
        """
        if not self._station_order:
            return None
        return self._station_order[self._current_station_idx]

    def _current_hand(self) -> str:
        """"L" or "R" -- whichever hand _current_station_side() currently is."""
        return "L" if self._current_station_side() == "left" else "R"

    async def _home_right(self, d: DriverSet) -> None:
        """
        Send HOME (always ends Righty) and record that the robot is
        physically Righty now. The ONLY thing that should ever set
        self._current_physical_side = "right" -- every existing
        move_to_safe_park() call site in this file goes through here
        instead, so the tracking can never drift from reality.
        """
        await d.robot.move_to_safe_park()
        self._current_physical_side = "right"

    async def _home_left(self, d: DriverSet) -> None:
        """
        Send GO_HOME_LEFT (always ends Lefty) and record that the robot
        is physically Lefty now. The ONLY thing that should ever set
        self._current_physical_side = "left".
        """
        await d.robot.go_home_left()
        self._current_physical_side = "left"

    async def _ensure_physical_hand_for_next_pick(self, d: DriverSet) -> None:
        """
        Cross hands FIRST if the robot's actual physical side doesn't
        match what the sequencer needs for the upcoming pick.

        Closes a real gap (2026-08-27, found via real-hardware testing):
        _startup() always homes Righty unconditionally, no matter which
        stations were selected. The explicit GO_HOME_LEFT crossing is
        normally inserted by _get_next_well_coords()'s side_switch signal
        -- but that only fires on a TRANSITION between stations mid-run.
        If the very FIRST selected station of a run is on the left side
        (e.g. only Station 3 selected, no right-side stations at all),
        there is no such transition to trigger it -- the sequencer
        correctly computes hand="L" for the very first pick, but nothing
        ever told the robot to actually get there, so it was trying to
        reach a left-side target directly from right_home in one bare
        command instead of the proven settle-then-sweep crossing.
        Called at the top of _run_cycle(), before overhead capture --
        cheap no-op on every cycle where the sides already match (the
        overwhelming majority: same-side continuation, or already
        correctly positioned by a prior side_switch crossing).
        """
        needed = self._current_station_side()
        if needed == self._current_physical_side:
            return
        log.info("cycle.correcting_physical_hand",
                 current=self._current_physical_side, needed=needed)
        if needed == "left":
            await self._home_left(d)
        else:
            await self._home_right(d)

    def _select_quadrant_u(self, target_y: float) -> float | None:
        """
        Pick the wrist U angle (deg) for a target's robot-Y position, so the
        arm can reach every quadrant of the dish.

        robot_y >= quadrant_split_y_mm -> physical top    -> top U
        robot_y <  quadrant_split_y_mm -> physical bottom -> bottom U

        Multi-station mode (self._station_order non-empty): the top/bottom
        U PAIR depends on _current_station_side() (Righty and Lefty need
        different U's to reach the same physical quadrant — see
        hardware_tests/test_quadrant_u_select.py). The split threshold
        itself (quadrant_split_y_mm) is a physical robot-Y boundary on the
        dish, unaffected by hand, so it's shared between both pairs.

        Legacy fallback (multi-station not configured): the original single
        Righty-only u_top_deg/u_bottom_deg pair, unchanged.

        Returns None if quadrant U-selection is not configured at all —
        callers should then omit u from move_to_image_site(), which
        preserves the pre-2026-08-05 behavior of holding whatever U the
        robot is currently at.
        """
        if self._quadrant_split_y_mm is None:
            return None

        if self._station_order and self._plate_stations_cfg is not None:
            top, bottom = (
                (self._plate_stations_cfg.righty_top_u, self._plate_stations_cfg.righty_bottom_u)
                if self._current_station_side() == "right"
                else (self._plate_stations_cfg.lefty_top_u, self._plate_stations_cfg.lefty_bottom_u)
            )
            return top if target_y >= self._quadrant_split_y_mm else bottom

        if self._u_top_deg is None or self._u_bottom_deg is None:
            return None
        return (
            self._u_top_deg
            if target_y >= self._quadrant_split_y_mm
            else self._u_bottom_deg
        )

    def _load_eoa_pixel_scale(self, target_u: float | None) -> dict | None:
        """
        Load (and cache) the EoA pixel-scale calibration for a specific U angle.

        The calibration is only valid at the U it was measured at — the EoA
        camera physically rotates with U, so a matrix measured at one angle
        does not correctly convert pixel offsets at another (see DEV-13,
        2026-08-19: this was the actual root cause of PICK staying off in X
        no matter how much the pipette tool offset was adjusted — the tool
        offset was fine, the vision correction feeding it a target was not).

        target_u = u_top_deg or u_bottom_deg  -> eoa_pixel_scale_u<target_u>.json
        target_u = None (quadrant U-selection not configured) -> the old
            single eoa_pixel_scale.json, for backward compatibility.
        Falls back to the single file (with a warning) if a U-specific file
        is requested but hasn't been calibrated yet.
        """
        import json

        if target_u in self._eoa_pixel_scale:
            return self._eoa_pixel_scale[target_u]

        out_dir = Path(__file__).resolve().parents[2] / "camera_calibration" / "output"
        if target_u is not None:
            cal_path = out_dir / f"eoa_pixel_scale_u{target_u:g}.json"
            if not cal_path.exists():
                log.warning("cycle.eoa_correction_no_u_specific_cal",
                            path=str(cal_path), target_u=target_u,
                            note="No calibration for this U angle — falling back to the "
                                 "un-quadranted eoa_pixel_scale.json (may be wrong for this "
                                 "quadrant). Run: python hardware_tests/eoa_pixel_scale.py "
                                 f"--u {target_u}")
                cal_path = out_dir / "eoa_pixel_scale.json"
        else:
            cal_path = out_dir / "eoa_pixel_scale.json"

        if not cal_path.exists():
            log.warning("cycle.eoa_correction_no_cal",
                        path=str(cal_path),
                        note="No EoA pixel-scale calibration found — using overhead centroid "
                             "as pick point. Run hardware_tests/eoa_pixel_scale.py --u <angle> "
                             "to calibrate.")
            self._eoa_pixel_scale[target_u] = None
            return None

        with open(cal_path) as f:
            cal = json.load(f)
        self._eoa_pixel_scale[target_u] = cal
        log.info("cycle.eoa_pixel_scale_loaded",
                 path=str(cal_path), target_u=target_u,
                 cal_u_deg=cal.get("u_deg"),
                 px_per_mm=cal.get("px_per_mm"),
                 um_per_px=round(cal.get("mm_per_px", 0) * 1000, 2))
        return cal

    def _compute_eoa_correction(
        self,
        body_center_px: tuple[float, float],
        frame_shape: tuple,
        target_x: float,
        target_y: float,
        target_u: float | None = None,
    ) -> tuple[float, float]:
        """
        Compute the corrected robot position so the bug body centre lands at the
        EoA image centre (= robot tool tip).

        Uses the signed 2×2 calibration matrix from the per-U eoa_pixel_scale
        file (see _load_eoa_pixel_scale — DEV-13):
            M @ [drob_x, drob_y]^T = [dcam_x, dcam_y]^T
        where camera_axis_x / delta_mm gives the camera-pixel shift per mm of
        robot X motion, and similarly for Y.

        To bring body_center_px to image centre:
            camera_shift_needed = (W/2 - bx, H/2 - by)
            robot_delta         = M_inv @ camera_shift_needed
            corrected           = target + robot_delta

        Returns (corrected_x, corrected_y) or (target_x, target_y) if no
        calibration file is available for this U angle.
        """
        import numpy as np

        cal = self._load_eoa_pixel_scale(target_u)
        if cal is None:
            return target_x, target_y

        delta = cal["delta_mm"]
        dx_x, dy_x = cal["camera_axis_x"]   # camera pixel shift per delta_mm robot X
        dx_y, dy_y = cal["camera_axis_y"]   # camera pixel shift per delta_mm robot Y

        # M maps [robot_dx_mm, robot_dy_mm] → [camera_dpx, camera_dpy]
        M = np.array(
            [[dx_x / delta, dx_y / delta],
             [dy_x / delta, dy_y / delta]],
            dtype=float,
        )
        M_inv = np.linalg.inv(M)

        H, W = frame_shape[:2]
        bx, by = body_center_px
        # Camera shift needed to bring body centre to image centre
        cam_shift   = np.array([W / 2.0 - bx, H / 2.0 - by], dtype=float)
        robot_delta = M_inv @ cam_shift

        corrected_x = target_x + float(robot_delta[0])
        corrected_y = target_y + float(robot_delta[1])

        log.info("cycle.eoa_correction",
                 body_px=f"({bx:.1f}, {by:.1f})",
                 image_centre=f"({W/2:.0f}, {H/2:.0f})",
                 pixel_offset=f"({bx - W/2:+.1f}, {by - H/2:+.1f})",
                 robot_delta_mm=f"({robot_delta[0]:+.3f}, {robot_delta[1]:+.3f})",
                 overhead_xy=f"({target_x:.3f}, {target_y:.3f})",
                 corrected_xy=f"({corrected_x:.3f}, {corrected_y:.3f})")

        return corrected_x, corrected_y

    def _stations_exhausted(self) -> bool:
        """
        True if multi-station mode is active (configure_pre_run() called)
        and every selected station's every well has already been used.

        Checked at the top of _run_cycle() BEFORE committing to a pick —
        there's no point aspirating a specimen with nowhere left to drop
        it. Always False when multi-station mode isn't active — the
        legacy single-plate _get_next_well_coords() fallback below wraps
        forever instead of ever reporting exhaustion.
        """
        return bool(self._station_order) and self._current_station_idx >= len(self._station_order)

    def _get_next_well_coords(self) -> tuple[float, float, str, float | None, str, bool, int | None]:
        """
        Return (well_x, well_y, well_label, well_home_u, side, side_switch,
        station_num) for the next well to drop into, advancing the internal
        sequencer.

        well_home_u is ALWAYS the current side's HOME U (right_home_u/
        left_home_u from machine_config.yaml's plate_stations section) —
        NEVER the quadrant-pick U used for this cycle's PICK. Confirmed
        operator safety rule: dropping at a quadrant-pick U is not safe.

        side_switch is True only when the NEXT call to this method (the
        well after the one just returned) will be for a different side —
        signals _run_cycle() that after finishing THIS drop, it must
        explicitly cross hands (go_home_left()) before the next overhead
        capture, instead of the normal same-side concurrent-lookahead trick.

        station_num is the station THIS well actually came from (None in
        the legacy fallback). 2026-09-07 bug fix: callers must use this
        return value instead of separately calling _current_station_number()
        afterward — whenever the well just returned was the LAST well of
        its station, this call already advances self._current_station_idx
        past it (needed so _stations_exhausted() sees the right thing next
        time), so a later _current_station_number()/_current_station_side()
        call in the same cycle would silently read the WRONG (next) station
        for every station boundary, and outright IndexError -- "list index
        out of range" -- crash the whole cycle at the very last well of the
        very last selected station (self._current_station_idx left pointing
        one past the end of self._station_order, nothing left to index).
        Found live on real hardware: single station selected, crashed
        exactly on well 96/96.

        Multi-station mode (configure_pre_run() called, self._station_order
        non-empty): fills the selected stations in the order built by
        configure_pre_run() (right side fully, then left side fully).
        Raises RuntimeError if every well is already used — callers must
        check _stations_exhausted() before committing to a pick; this
        should never actually be hit.

        Legacy fallback (multi-station not configured): sequential fill
        order from the single loaded self._plate_config, wrapping back to
        the first well when the plate is full (2026-08-11 test-scoped
        behavior, unchanged — see prd_updated.md Appendix D). Falls back
        further to the old hardcoded (100.0, 100.0) placeholder if no
        plate config was loaded at all (mock/dev runs without a real plate).
        """
        if self._station_order:
            if self._current_station_idx >= len(self._station_order):
                raise RuntimeError(
                    "_get_next_well_coords() called after all stations were "
                    "already exhausted -- caller must check _stations_exhausted() first."
                )
            station_num = self._station_order[self._current_station_idx]
            plate = self._selected_stations[station_num]
            well = plate.wells[self._current_well_idx]
            side = self._plate_stations_cfg.side_of(station_num)
            home_u = (
                self._plate_stations_cfg.right_home_u if side == "right"
                else self._plate_stations_cfg.left_home_u
            )

            self._current_well_idx += 1
            side_switch = False
            if self._current_well_idx >= len(plate.wells):
                self._current_well_idx = 0
                self._current_station_idx += 1
                if self._current_station_idx < len(self._station_order):
                    next_station = self._station_order[self._current_station_idx]
                    next_side = self._plate_stations_cfg.side_of(next_station)
                    side_switch = next_side != side
            return (well.x, well.y, well.well, home_u, side, side_switch, station_num)

        if self._plate_config is None or not self._plate_config.wells:
            return (100.0, 100.0, "NONE", self._plate_u, "right", False, None)

        wells = self._plate_config.wells
        well = wells[self._next_well_index % len(wells)]
        if self._next_well_index >= len(wells):
            log.warning("cycle.plate_wrapped",
                        note="well index wrapped past the end of the plate — "
                             "test-scoped behavior, not correct for production")
        self._next_well_index += 1
        return (well.x, well.y, well.well, self._plate_u, "right", False, None)

    # ------------------------------------------------------------------
    # VIEW helpers — image display for testing (VIEW=True in main.py)
    # ------------------------------------------------------------------
    # NOTE: cv2.imshow/waitKey deadlocks on Windows when PyQt owns the
    # main thread message pump. Images are saved to disk instead; use
    # the saved file paths for review.

    def _show_image(self, img: Any, title: str) -> None:
        """Display an image for VIEW=True debugging — self-closing preview.

        Was os.startfile() until 2026-08-18: that opens a brand new external
        Photos/viewer process EVERY cycle and never closes any of them — the
        module docstring's own "3s" comment describes a timed auto-closing
        preview that this never actually implemented. Over a long session
        that's dozens of full-size (73MB decoded) viewer windows accumulating
        indefinitely, which was very likely the dominant contributor to a
        live OpenCV OutOfMemoryError (on top of the internal buffer reuse
        fixed separately in cib/vision/pipeline.py — see draw_target_highlight).
        cv2.imshow()/waitKey()/destroyWindow() shows the same preview inline,
        in-process, and guarantees it's gone again after ~3s, every time.
        """
        import cv2

        h, w = img.shape[:2]
        log.info("view.image", title=title, shape=f"{w}x{h}")
        try:
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(title, min(1400, w), min(1000, h))
            cv2.imshow(title, img)
            cv2.waitKey(3000)
        except Exception as exc:
            log.warning("view.image_show_failed", error=str(exc))
        finally:
            try:
                cv2.destroyWindow(title)
            except Exception:
                pass

    def _show_image_path(self, path: str, title: str) -> None:
        """Display a saved image for VIEW=True debugging — self-closing preview.

        See _show_image() — same fix, same reasoning (was os.startfile()).
        """
        import cv2

        if not path:
            return
        log.info("view.image_saved", title=title, path=path)
        img = cv2.imread(path)
        if img is None:
            log.warning("view.image_show_failed", path=path, error="cv2.imread returned None")
            return
        try:
            h, w = img.shape[:2]
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(title, min(1400, w), min(1000, h))
            cv2.imshow(title, img)
            cv2.waitKey(3000)
        except Exception as exc:
            log.warning("view.image_show_failed", path=path, error=str(exc))
        finally:
            try:
                cv2.destroyWindow(title)
            except Exception:
                pass
