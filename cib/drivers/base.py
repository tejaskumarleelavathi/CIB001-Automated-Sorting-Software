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

    async def move_to_image_site(self, x: float, y: float) -> None:
        """Move to pick site for EoA close-up image."""
        ...

    async def execute_pick_place(
        self,
        pick_x: float, pick_y: float,
        well_x: float, well_y: float,
    ) -> None:
        """Full pick from refined coords and place into well."""
        ...

    async def move_to_safe_park(self) -> None:
        """Move to parked/home position."""
        ...

    async def halt(self) -> None:
        """Immediate stop — called on safety fault."""
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


# ---------------------------------------------------------------------------
# Logic PLC — lights + event log only
# ---------------------------------------------------------------------------

class StatusLight:
    GREEN  = "green"
    AMBER  = "amber"
    RED    = "red"
    OFF    = "off"


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
