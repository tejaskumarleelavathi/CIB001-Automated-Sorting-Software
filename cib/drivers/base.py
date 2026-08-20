"""
Abstract interfaces for every hardware driver.

Every real driver AND every mock driver must implement the Protocol defined
here. If you add a new method to a Protocol, the type checker will immediately
flag every driver (real + mock) that doesn't implement it.

To add a new device:
    1. Define its Protocol here
    2. Create cib/drivers/<device>/driver.py  (real)
    3. Create cib/drivers/<device>/mock.py    (mock)
    4. Wire it into the orchestrator
    Nothing else needs to change.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
import numpy as np


# ---------------------------------------------------------------------------
# Robot
# ---------------------------------------------------------------------------

@runtime_checkable
class RobotDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_ready(self) -> bool: ...

    async def move_to_image_site(self, x: float, y: float, u: float | None = None) -> None:
        """
        Move to pick site for EoA close-up image.

        u: optional wrist angle (deg). If given, the robot moves to this exact
        U (quadrant-based U selection). If None, holds whatever U the robot is
        currently at (pre-2026-08-05 behavior).
        """
        ...

    async def execute_pick(
        self, pick_x: float, pick_y: float, u: float | None = None,
    ) -> None:
        """
        Aspirate the specimen at (pick_x, pick_y) using the pipette (Tool 2).
        Returns once the specimen is confirmed in the tip.

        u: wrist angle (deg) for the pick move — same u used for the EoA
        move that found this specimen (dish quadrant selection).

        Split from the drop step (execute_place) so the orchestrator can
        record "specimen is now in the pipette" as its own durable state
        between the two calls — critical for e-stop/light-curtain recovery,
        which must know whether a specimen was already aspirated when the
        fault hit, so it can deliver it instead of losing it or attempting
        a second pick on top of it.
        """
        ...

    async def execute_place(
        self, well_x: float, well_y: float, well_u: float | None = None,
    ) -> None:
        """
        Travel to (well_x, well_y) and dispense the specimen currently held
        in the pipette (Tool 2).

        well_u: wrist angle (deg) for the drop move. The plate station is a
        different physical location from the dish and needs its own U —
        do not reuse the pick's u here. None holds whatever U the arm is
        currently at.
        """
        ...

    async def move_to_safe_park(self) -> None:
        """Move to parked/home position."""
        ...

    async def halt(self) -> None:
        """Immediate stop — called on safety fault."""
        ...

    def resume(self) -> None:
        """
        Clear the halted flag after a safety fault physically clears.
        Must be called before any further robot command will be attempted —
        the driver refuses to reconnect while halted (see driver.py).
        Synchronous — does not itself reconnect or move anything.
        """
        ...

    async def keepalive(self) -> None:
        """Send a no-motion ping to prevent the controller dropping an idle connection."""
        ...


# ---------------------------------------------------------------------------
# Safety controller (Pilz Flexi Compact) — READ ONLY
# ---------------------------------------------------------------------------

@runtime_checkable
class SafetyDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def read_status(self) -> dict[str, Any]:
        """
        Returns dict with keys:
            any_fault_active: bool
            light_curtain_tripped: bool
            estop_active: bool
            estop_button_index: int | None
            contactor_closed: bool
            reset_available: bool
        """
        ...

    async def send_reset_acknowledge(self) -> None:
        """
        Release the light curtain latch. Only called when the curtain is
        physically clear AND the operator has pressed CONTINUE.
        """
        ...


# ---------------------------------------------------------------------------
# Logic PLC — lights + event log only
# ---------------------------------------------------------------------------

class StatusLight:
    GREEN     = "green"       # steady green  — idle/ready, or e-stop/curtain just cleared
    RUNNING   = "running"     # chasing green — actively running
    AMBER     = "amber"       # steady amber  — cycle stopped / aborted, awaiting operator
    RED       = "red"         # steady red    — e-stop active
    RED_FLASH = "red_flash"   # flashing red  — light curtain tripped (protective stop)
    RED_CHASE = "red_chase"   # chasing red   — connection lost / unrecoverable fault
    OFF       = "off"


@runtime_checkable
class PLCDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def set_status_light(self, colour: str) -> None: ...
    async def set_work_light(self, on: bool) -> None: ...
    async def read_event_log(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Syringe pump (IMS M-Drive)
# ---------------------------------------------------------------------------

@runtime_checkable
class PumpDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def aspirate(self, volume_ul: float, speed: float) -> None: ...
    async def dispense(self, volume_ul: float, speed: float) -> None: ...
    async def is_ready(self) -> bool: ...


# ---------------------------------------------------------------------------
# Camera (overhead + EoA — same interface, different instances)
# ---------------------------------------------------------------------------

@runtime_checkable
class CameraDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def start_streaming(self) -> None:
        """Begin frame acquisition. No-op if already streaming."""
        ...
    async def stop_streaming(self) -> None:
        """Halt frame acquisition. No-op if already stopped."""
        ...
    async def capture(self) -> Any:
        """Returns numpy ndarray (raw frame). Stream must be running."""
        ...
    async def set_exposure(self, exposure_us: float) -> None:
        """Change exposure time in µs. Takes effect on the next frame."""
        ...


# ---------------------------------------------------------------------------
# Lighting controller
# ---------------------------------------------------------------------------

@runtime_checkable
class LightingDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def set_backlight(self, on: bool) -> None: ...
    async def set_ring_light(self, on: bool) -> None: ...
