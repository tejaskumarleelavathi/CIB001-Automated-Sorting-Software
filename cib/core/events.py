"""
All events that flow through the system.

Every piece of information moving between a device driver thread and the
orchestrator is typed here. Nothing is passed as raw strings or dicts.
Adding a new event = add a dataclass here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any
import numpy as np

from cib.config.loader import PlateConfig


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SafetyFaultType(Enum):
    ESTOP            = auto()
    LIGHT_CURTAIN    = auto()
    CONTACTOR_OPEN   = auto()
    CONNECTION_LOSS  = auto()


class CyclePhase(Enum):
    """
    Tracks where in the operating cycle we are at any moment.
    Used by _safe_recovery() to decide what cleanup is needed after a fault.
    Set by the orchestrator throughout _run_cycle(). Reset to IDLE after recovery.

    Current phases (pick-place not yet implemented):
      IDLE              — between cycles or not running
      OVERHEAD_CAPTURE  — streaming overhead camera, capturing frame
      OVERHEAD_VISION   — running overhead blob detection (thread pool)
      ROBOT_MOVING      — robot travelling to EoA image site
      EOA_CAPTURE       — streaming EoA camera, capturing frames
      EOA_VISION        — running EoA classifier (thread pool)
      ROBOT_PARKING     — robot returning to safe park (HOME)

    Future phases (wire in when pick-place is implemented):
      ROBOT_PICK        — robot has lowered pipette tip to specimen
      PUMP_ASPIRATING   — pump pulling specimen into tip (bug in tip here!)
      ROBOT_TO_WELL     — robot carrying specimen to target well
      PUMP_DISPENSING   — pump dispensing specimen into well
    """
    IDLE             = auto()
    OVERHEAD_CAPTURE = auto()
    OVERHEAD_VISION  = auto()
    ROBOT_MOVING     = auto()
    EOA_CAPTURE      = auto()
    EOA_VISION       = auto()
    ROBOT_PARKING    = auto()
    # Pick-place phases — not yet implemented
    ROBOT_PICK       = auto()
    PUMP_ASPIRATING  = auto()
    ROBOT_TO_WELL    = auto()
    PUMP_DISPENSING  = auto()


class DeviceName(Enum):
    ROBOT    = "Robot"
    SAFETY   = "Flexi Compact"
    PLC      = "Logic PLC"
    PUMP     = "M-Drive Pump"
    CAMERA_OVERHEAD = "Overhead Camera"
    CAMERA_EOA      = "EoA Camera"
    LIGHTING = "Lighting Controller"


class CameraID(Enum):
    OVERHEAD = auto()
    EOA      = auto()


class PumpAction(Enum):
    ASPIRATE = auto()
    DISPENSE = auto()


# ---------------------------------------------------------------------------
# Operator / UI events  (UI → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatorStartEvent:
    pass

@dataclass(frozen=True)
class OperatorStopEvent:
    """Graceful cycle stop — finish current cycle then halt."""
    pass

@dataclass(frozen=True)
class OperatorAbortEvent:
    """Immediate abort — clear run data, return to idle."""
    pass

@dataclass(frozen=True)
class OperatorAcknowledgeEvent:
    """Operator pressed START/CONTINUE after e-stop or protective stop."""
    pass

@dataclass(frozen=True)
class OperatorShutdownEvent:
    """Operator pressed SHUTDOWN — disconnect all hardware and exit cleanly."""
    pass

@dataclass(frozen=True)
class OperatorConfigurePreRunEvent:
    """
    Operator filled out the pre-run setup screen (field code, per-station
    Plate ID, optional per-station starting well, Excel upload + station
    map taps). Handled while IDLE, before START -- see
    Orchestrator.configure_pre_run(). Supersedes the older
    OperatorConfigureStationsEvent (2026-08-27, FR-UI-01) -- Plate ID and
    starting well are new; selected/field_code carry the same meaning as
    before.

    field_code: this run's field collection code (Run.field_code).
    plate_ids: station number -> Plate ID text (Pick.plate_id -- per
        station, not per run, since each station holds a physically
        different plate).
    starting_wells: station number -> well label to resume from (e.g.
        "C4"), or None to start from the first well -- passed to
        PlateConfig.wells_from() when building the fill sequence.
    selected: station number -> PlateConfig, already loaded from the
        uploaded workbook. Only stations present here are filled --
        station map taps determine which stations end up in this dict.
    """
    field_code: str
    plate_ids: dict[int, str]
    starting_wells: dict[int, str | None]
    selected: dict[int, PlateConfig]


# ---------------------------------------------------------------------------
# Safety events  (Flexi Compact → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafetyFaultEvent:
    fault_type: SafetyFaultType
    estop_button_index: int | None = None   # which e-stop button (if ESTOP)
    detail: str = ""

@dataclass(frozen=True)
class SafetyClearEvent:
    """Hardware reset complete — safety circuit operational again."""
    pass

@dataclass(frozen=True)
class SafetyStatusEvent:
    """Periodic 100ms status snapshot from Flexi Compact."""
    any_fault_active: bool
    light_curtain_tripped: bool
    estop_active: bool
    estop_button_index: int | None
    contactor_closed: bool
    reset_available: bool           # blue reset light flashing


# ---------------------------------------------------------------------------
# Robot events  (Robot → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RobotConnectedEvent:
    pass

@dataclass(frozen=True)
class RobotInPositionEvent:
    pass

@dataclass(frozen=True)
class RobotMissionCompleteEvent:
    pass

@dataclass(frozen=True)
class RobotClearedFOVEvent:
    """Robot has moved outside overhead camera field of view — trigger next image."""
    pass

@dataclass(frozen=True)
class RobotFaultEvent:
    detail: str


# ---------------------------------------------------------------------------
# Camera events  (Camera → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameReadyEvent:
    camera_id: CameraID
    frame: Any          # numpy ndarray — typed as Any to avoid numpy import issues


# ---------------------------------------------------------------------------
# Vision events  (Vision pipeline → orchestrator, runs in thread pool)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VisionOverheadResultEvent:
    """Result of overhead image analysis."""
    centroids_robot_xy: list[tuple[float, float]]   # in robot coordinate space
    centroids_px: list[tuple[float, float]]          # pixel coords, paired 1:1 with centroids_robot_xy
    annotated_frame: Any = None                      # numpy array — always populated by pipeline
    annotated_frame_path: str = ""
    sample_count: int = 0

@dataclass(frozen=True)
class VisionEoAResultEvent:
    """Result of EoA close-up analysis."""
    proceed_with_pick: bool
    refined_x: float | None       # robot mm — None if no EoA calibration yet
    refined_y: float | None       # robot mm — None if no EoA calibration yet
    frame_path: str
    label: str = "UNKNOWN"        # BUG | DEBRIS | HAIR | NO_SPECIMEN | UNKNOWN
    reason: str = ""              # classifier reason string (for logs/UI)
    body_center_px: tuple = ()    # (px, py) in EoA image — empty if no specimen
    offset_from_center_px: tuple = ()  # (dx, dy) from image centre — how far body is from where robot went


# ---------------------------------------------------------------------------
# Pump events  (M-Drive → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PumpActionCompleteEvent:
    action: PumpAction

@dataclass(frozen=True)
class PumpFaultEvent:
    detail: str


# ---------------------------------------------------------------------------
# PLC events  (Logic PLC → orchestrator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PLCConnectedEvent:
    pass

@dataclass(frozen=True)
class PLCEventLogEntry:
    entries: list[str]


# ---------------------------------------------------------------------------
# System / connection events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectionLostEvent:
    device: DeviceName
    detail: str = ""

@dataclass(frozen=True)
class AllDevicesReadyEvent:
    """Fired by orchestrator once all startup checks pass."""
    pass


@dataclass(frozen=True)
class SystemErrorEvent:
    """
    A human-readable description of an operational fault, for UI display.
    Pushed alongside sm.fault() at every non-safety fault point (robot
    unreachable after recovery, unrecovered cycle disconnect, etc.) --
    safety faults (e-stop/light curtain) have their own dedicated banner
    (SafetyStatusEvent) and don't need this. Distinct from the structured
    log (which always gets every error regardless) -- this is specifically
    for the one thing the terminal shows in red that the UI-only operator
    otherwise never sees.
    """
    message: str


@dataclass(frozen=True)
class RecoveryStatusEvent:
    """
    A short, human-readable progress update pushed while _safe_recovery()
    (post-safety-fault CONTINUE) is running (2026-08-31) -- e.g.
    "Reconnecting to robot...", "Restarting robot program...", "Homing
    robot...". The Remote Ethernet fallback restart this can now trigger
    (Stop/Reset/MotorsOn/Start/reconnect/rehome) takes up to ~30s, and
    without this the operator just watched the CONTINUE button do nothing
    for that whole window with no indication anything was happening.
    Distinct from SystemErrorEvent (styled red, implies something is
    wrong) -- this is routine progress, shown in the safety banner's own
    status line, not styled as an error.
    """
    message: str


@dataclass(frozen=True)
class EoaFrameCapturedEvent:
    """
    A fresh EoA (End-of-Arm) close-up frame, pushed every time one is
    captured during a cycle -- for the run view's camera panel, which
    shows "last EoA capture" rather than a continuous stream (see
    cib/ui/widgets/camera_feed.py's module docstring for why). Carries the
    no-backlight frame specifically (the one used for vision analysis,
    always captured every attempt regardless of pick outcome), not the
    ring-light archival frame.
    """
    frame: Any


@dataclass(frozen=True)
class RunStatusEvent:
    """
    Live run summary for the UI (FR-UI-09): current field code, pass/fail
    counts, and when the run started -- pushed whenever a run opens/closes
    (_ensure_run_open/_close_run) and after every pick finishes
    (_db_finish_pick). run_id=None means no run is currently open/resumable
    -- the UI uses this (not State alone) to decide whether IDLE means
    "fresh/aborted, show pre-run setup" or "paused mid-run, show CONTINUE."
    """
    run_id: str | None
    field_code: str
    started_at: datetime | None
    pass_count: int
    fail_count: int


# All event types in one union — used for type hints in the orchestrator
SystemEvent = (
    OperatorStartEvent | OperatorStopEvent | OperatorAbortEvent | OperatorAcknowledgeEvent
    | OperatorShutdownEvent | OperatorConfigurePreRunEvent
    | SafetyFaultEvent | SafetyClearEvent | SafetyStatusEvent
    | RobotConnectedEvent | RobotInPositionEvent | RobotMissionCompleteEvent
    | RobotClearedFOVEvent | RobotFaultEvent
    | FrameReadyEvent
    | VisionOverheadResultEvent | VisionEoAResultEvent
    | PumpActionCompleteEvent | PumpFaultEvent
    | PLCConnectedEvent | PLCEventLogEntry
    | ConnectionLostEvent | AllDevicesReadyEvent
    | SystemErrorEvent | RecoveryStatusEvent | EoaFrameCapturedEvent | RunStatusEvent
)
