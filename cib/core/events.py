"""
All events that flow through the system.

Every piece of information moving between a device driver thread and the
orchestrator is typed here. Nothing is passed as raw strings or dicts.
Adding a new event = add a dataclass here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
import numpy as np


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SafetyFaultType(Enum):
    ESTOP            = auto()
    LIGHT_CURTAIN    = auto()
    CONTACTOR_OPEN   = auto()
    CONNECTION_LOSS  = auto()


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
    refined_x: float | None
    refined_y: float | None
    frame_path: str


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


# All event types in one union — used for type hints in the orchestrator
SystemEvent = (
    OperatorStartEvent | OperatorStopEvent | OperatorAbortEvent | OperatorAcknowledgeEvent
    | OperatorShutdownEvent
    | SafetyFaultEvent | SafetyClearEvent | SafetyStatusEvent
    | RobotConnectedEvent | RobotInPositionEvent | RobotMissionCompleteEvent
    | RobotClearedFOVEvent | RobotFaultEvent
    | FrameReadyEvent
    | VisionOverheadResultEvent | VisionEoAResultEvent
    | PumpActionCompleteEvent | PumpFaultEvent
    | PLCConnectedEvent | PLCEventLogEntry
    | ConnectionLostEvent | AllDevicesReadyEvent
)
