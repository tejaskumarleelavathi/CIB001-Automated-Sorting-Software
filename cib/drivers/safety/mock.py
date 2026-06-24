"""
Mock Flexi Compact safety driver — for development on laptop.

Simulates a healthy safety system by default.
Tests can call inject_fault() to simulate e-stop / light curtain events.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class MockSafetyDriver:
    """
    Simulates the Pilz Flexi Compact.
    All faults start cleared. Use inject_* methods in tests to trigger faults.
    """

    def __init__(self) -> None:
        self._connected = False
        self._estop_active = False
        self._estop_button_index: int | None = None
        self._light_curtain_tripped = False
        self._contactor_closed = True
        self._reset_available = False

    async def connect(self) -> None:
        await asyncio.sleep(0.05)   # simulate connection delay
        self._connected = True
        log.info("mock_safety.connected")

    async def disconnect(self) -> None:
        self._connected = False
        log.info("mock_safety.disconnected")

    async def read_status(self) -> dict[str, Any]:
        await asyncio.sleep(0.005)  # simulate modbus round trip
        any_fault = self._estop_active or self._light_curtain_tripped or not self._contactor_closed
        return {
            "any_fault_active":       any_fault,
            "light_curtain_tripped":  self._light_curtain_tripped,
            "estop_active":           self._estop_active,
            "estop_button_index":     self._estop_button_index,
            "contactor_closed":       self._contactor_closed,
            "reset_available":        self._reset_available,
        }

    # ------------------------------------------------------------------
    # Test / simulation helpers
    # ------------------------------------------------------------------

    def inject_estop(self, button_index: int = 0) -> None:
        self._estop_active = True
        self._estop_button_index = button_index
        self._contactor_closed = False
        self._reset_available = False
        log.warning("mock_safety.estop_injected", button=button_index)

    def inject_light_curtain_trip(self) -> None:
        self._light_curtain_tripped = True
        log.warning("mock_safety.light_curtain_injected")

    def clear_estop(self) -> None:
        """Simulate operator releasing the e-stop button."""
        self._estop_active = False
        self._reset_available = True
        log.info("mock_safety.estop_cleared")

    def press_reset(self) -> None:
        """Simulate operator pressing the blue reset button."""
        self._reset_available = False
        self._contactor_closed = True
        log.info("mock_safety.reset_pressed")

    def clear_light_curtain(self) -> None:
        self._light_curtain_tripped = False
        log.info("mock_safety.light_curtain_cleared")
