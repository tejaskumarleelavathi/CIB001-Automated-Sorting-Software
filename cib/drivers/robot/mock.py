"""
Mock Epson T3-B robot driver — for development on laptop.

Simulates realistic timing (moves take time) so the orchestrator
and UI behave exactly as they would with real hardware.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)

MOCK_MOVE_SECONDS = 2.0     # simulate robot travel time
MOCK_PICK_SECONDS = 1.0     # simulate pick execution time


class MockRobotDriver:

    def __init__(self) -> None:
        self._connected = False
        self._halted = False

    async def connect(self) -> None:
        await asyncio.sleep(0.1)
        self._connected = True
        log.info("mock_robot.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def is_ready(self) -> bool:
        return self._connected and not self._halted

    async def move_to_image_site(self, x: float, y: float) -> None:
        log.info("mock_robot.move_to_image_site", x=x, y=y)
        await asyncio.sleep(MOCK_MOVE_SECONDS)
        log.info("mock_robot.in_position")

    async def execute_pick_place(
        self,
        pick_x: float, pick_y: float,
        well_x: float, well_y: float,
    ) -> None:
        log.info("mock_robot.pick_place_start", pick_x=pick_x, pick_y=pick_y,
                 well_x=well_x, well_y=well_y)
        await asyncio.sleep(MOCK_PICK_SECONDS)
        log.info("mock_robot.pick_place_complete")

    async def move_to_safe_park(self) -> None:
        log.info("mock_robot.parking")
        await asyncio.sleep(MOCK_MOVE_SECONDS)
        log.info("mock_robot.parked")

    async def halt(self) -> None:
        self._halted = True
        log.warning("mock_robot.halted")

    def resume(self) -> None:
        """Call after safety reset to allow motion again."""
        self._halted = False
