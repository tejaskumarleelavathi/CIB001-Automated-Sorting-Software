"""
Mock Epson T3-B robot driver — for development on laptop.

Simulates realistic timing (moves take time) so the orchestrator
and UI behave exactly as they would with real hardware.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import structlog

log = structlog.get_logger(__name__)

MOCK_MOVE_SECONDS = 2.0     # simulate robot travel time
MOCK_PICK_SECONDS = 1.0     # simulate pick execution time


class MockRobotDriver:

    def __init__(self) -> None:
        self._connected = False
        self._halted = False
        self._reset_pipette_call_count = 0   # lets tests assert startup calls this once

    async def connect(self) -> None:
        await asyncio.sleep(0.1)
        self._connected = True
        log.info("mock_robot.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def is_ready(self) -> bool:
        return self._connected and not self._halted

    async def move_to_image_site(
        self, x: float, y: float, u: float | None = None, hand: str = "R",
    ) -> None:
        log.info("mock_robot.move_to_image_site", x=x, y=y, u=u, hand=hand)
        await asyncio.sleep(MOCK_MOVE_SECONDS)
        log.info("mock_robot.in_position")

    async def execute_pick(
        self, pick_x: float, pick_y: float, u: float | None = None, hand: str = "R",
        on_aspirated: Callable[[], None] | None = None,
    ) -> None:
        """
        Mirrors the real driver's two-phase timing (2026-08-31): fires
        on_aspirated partway through, before the simulated retract/
        travel-back completes, so orchestrator-level tests can exercise
        the same "specimen recorded before this whole call returns"
        behavior without needing real hardware.
        """
        log.info("mock_robot.pick_start", pick_x=pick_x, pick_y=pick_y, u=u, hand=hand)
        await asyncio.sleep(MOCK_PICK_SECONDS * 0.5)
        log.info("mock_robot.aspirated")
        if on_aspirated is not None:
            on_aspirated()
        await asyncio.sleep(MOCK_PICK_SECONDS * 0.5)
        log.info("mock_robot.pick_complete")

    async def execute_place(
        self, well_x: float, well_y: float, well_u: float | None = None, hand: str = "R",
    ) -> None:
        log.info("mock_robot.place_start", well_x=well_x, well_y=well_y, well_u=well_u, hand=hand)
        await asyncio.sleep(MOCK_PICK_SECONDS)
        log.info("mock_robot.place_complete")

    async def move_to_safe_park(self) -> None:
        log.info("mock_robot.parking")
        await asyncio.sleep(MOCK_MOVE_SECONDS)
        log.info("mock_robot.parked")

    async def go_home_left(self) -> None:
        log.info("mock_robot.parking_left")
        await asyncio.sleep(MOCK_MOVE_SECONDS)
        log.info("mock_robot.parked_left")

    async def ring_light_on(self) -> None:
        log.info("mock_robot.ring_light_on")

    async def ring_light_off(self) -> None:
        log.info("mock_robot.ring_light_off")

    async def reset_pipette(self) -> None:
        self._reset_pipette_call_count += 1
        log.info("mock_robot.pipette_reset")

    async def halt(self) -> None:
        self._halted = True
        log.warning("mock_robot.halted")

    async def keepalive(self) -> None:
        pass  # no-op — mock has no TCP connection to keep alive

    def resume(self) -> None:
        """Call after safety reset to allow motion again."""
        self._halted = False
