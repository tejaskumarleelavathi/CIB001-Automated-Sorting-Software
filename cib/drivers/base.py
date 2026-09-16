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

from typing import Any, Callable, Protocol, runtime_checkable
import numpy as np


# ---------------------------------------------------------------------------
# Robot
# ---------------------------------------------------------------------------

@runtime_checkable
class RobotDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_ready(self) -> bool: ...

    async def move_to_image_site(
        self, x: float, y: float, u: float | None = None, hand: str = "R",
    ) -> None:
        """
        Move to pick site for EoA close-up image.

        u: optional wrist angle (deg). If given, the robot moves to this exact
        U (quadrant-based U selection). If None, holds whatever U/hand the
        robot is currently at (pre-2026-08-05 behavior) — hand is ignored.

        hand: "L" or "R" — which side's quadrant U this is. Only meaningful
        when u is given.
        """
        ...

    async def execute_pick(
        self, pick_x: float, pick_y: float, u: float | None = None, hand: str = "R",
        on_aspirated: Callable[[], None] | None = None,
    ) -> None:
        """
        Aspirate the specimen at (pick_x, pick_y) using the pipette (Tool 2).
        Returns once the specimen is confirmed in the tip AND the robot has
        retracted/returned to travel height.

        u: wrist angle (deg) for the pick move — same u used for the EoA
        move that found this specimen (dish quadrant selection).
        hand: "L" or "R" — which side this quadrant U belongs to. Only
        meaningful when u is given.

        on_aspirated: optional zero-arg callback invoked the instant the
        specimen is confirmed in the tip — BEFORE the robot has also
        retracted/traveled back. Safe to touch orchestrator state from
        directly. Lets the orchestrator record "specimen is now in the
        pipette" at the true earliest correct moment (2026-08-31,
        real-hardware finding: recording it only after this whole call
        returns left a real race where a safety fault during the
        retract/travel-back — after the specimen was genuinely already
        aspirated — could cut the connection before this call ever
        returned, leaving an already-aspirated specimen unrecorded).

        Split from the drop step (execute_place) so the orchestrator can
        record "specimen is now in the pipette" as its own durable state
        between the two calls — critical for e-stop/light-curtain recovery,
        which must know whether a specimen was already aspirated when the
        fault hit, so it can deliver it instead of losing it or attempting
        a second pick on top of it.
        """
        ...

    async def execute_place(
        self, well_x: float, well_y: float, well_u: float | None = None, hand: str = "R",
    ) -> None:
        """
        Travel to (well_x, well_y) and dispense the specimen currently held
        in the pipette (Tool 2).

        well_u: wrist angle (deg) for the drop move. The plate station is a
        different physical location from the dish and needs its own U —
        do not reuse the pick's u here. None holds whatever U the arm is
        currently at. Caller must always pass the current side's HOME U
        (never a quadrant-pick U) — hard safety rule.
        hand: "L" or "R" — which side's plate station this is. Only
        meaningful when well_u is given.
        """
        ...

    async def move_to_safe_park(self) -> None:
        """Move to parked/home position (always ends Righty at right home)."""
        ...

    async def go_home_left(self) -> None:
        """Move to left home position (always ends Lefty at left home)."""
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

    async def reset_pipette(self) -> None:
        """
        Motor-only safety release: homes the pipette plunger against its
        dispense-end hard stop (low current + closed-loop stall detection,
        not a blind push), no robot arm travel. Called once by the
        orchestrator at every startup so a specimen can never be silently
        left in the tip from a crashed/interrupted previous run. Takes
        noticeably longer than a plain drop -- a real seek move, not an
        instant trigger.
        """
        ...


# ---------------------------------------------------------------------------
# Robot Remote Ethernet — controller-level lifecycle control
# (Login/Reset/SetMotorsOn/Off/Start/Stop), separate from RobotDriver's
# own PICK/DROP/MOVE_IMAGE protocol above. See cib/drivers/robot_remote/.
# Deliberately not held open for the process lifetime — connect(), run a
# short sequence, disconnect().
# ---------------------------------------------------------------------------

@runtime_checkable
class RobotRemoteDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_status(self) -> Any: ...
    async def wait_until_ready(self, timeout_s: float = 30.0, poll_interval_s: float = 0.2) -> bool: ...
    async def wait_until_error_cleared(self, timeout_s: float = 8.0, poll_interval_s: float = 0.2) -> bool: ...
    async def wait_until_running(self, timeout_s: float = 8.0, poll_interval_s: float = 0.2) -> bool: ...
    async def reset(self, robot_number: int = 0) -> None: ...
    async def set_motors_on(self, robot_number: int = 0) -> None: ...
    async def set_motors_off(self, robot_number: int = 0) -> None: ...
    async def start(self, function_no: int = 0) -> None: ...
    async def stop(self, robot_number: int = 0) -> None: ...
    async def home(self) -> None: ...


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
    GREEN       = "green"       # steady green   — idle/ready, or e-stop/curtain just cleared
    RUNNING     = "running"     # chasing green  — actively running
    AMBER       = "amber"       # steady amber   — cycle currently stopping (transient, mid-shutdown-of-cycle)
    RED         = "red"         # steady red     — e-stop active
    RED_FLASH   = "red_flash"   # flashing red   — light curtain tripped (protective stop)
    RED_CHASE   = "red_chase"   # chasing red    — connection lost / unrecoverable fault
    WHITE_FLASH = "white_flash" # flashing white — operator shutdown in progress (2026-09-16)
    OFF         = "off"


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

    async def lock_white_balance(self) -> bool:
        """
        Converge white balance via BalanceWhiteAuto="Once" — the
        GenICam-standard self-terminating auto function — and hold those
        gains fixed once the camera itself reports convergence (resets the
        node to "Off" on its own). True/False = confirmed converged or not;
        on False the camera is left in Continuous mode as a safe fallback,
        not a half-finished "Once" state. No-op (True) on cameras with no
        WB nodes (e.g. Mono8).

        Added 2026-08-21 (optimize-speed pass), REWRITTEN same day after
        the first version (Continuous for a guessed frame count, then
        forced to Off) produced badly overexposed real captures — see
        driver.py for the full story. Call once per camera connection,
        under the real capture lighting; skip re-calling once it returns
        True (see cib/core/orchestrator.py's self._eoa_wb_locked).
        """
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

    async def set_backlight_channels(self, channels: list[int], on: bool) -> None:
        """
        Turn specific backlight channels on/off independently, leaving the
        others untouched. DCS-100E has 3 channels (1-3); set_backlight()
        treats all of them as one on/off unit, this doesn't.

        Added 2026-08-21 (optimize-speed pass) so the EoA with-backlight
        capture can run at reduced brightness (fewer than 3 channels) —
        operator's own observation that the specimen was overexposed with
        all 3 on. Bring the remaining channels up afterward (still via this
        method) to reach full brightness for the next cycle's overhead
        capture, which does need all 3.
        """
        ...
