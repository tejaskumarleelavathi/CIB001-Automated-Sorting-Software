"""Mock lighting controller driver."""

from __future__ import annotations
import asyncio
import structlog

log = structlog.get_logger(__name__)


class MockLightingDriver:

    def __init__(self) -> None:
        self._connected = False
        self.backlight_on = False
        self.ring_light_on = False

    async def connect(self) -> None:
        await asyncio.sleep(0.02)
        self._connected = True
        log.info("mock_lighting.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def set_backlight(self, on: bool) -> None:
        self.backlight_on = on
        log.debug("mock_lighting.backlight", on=on)

    async def set_ring_light(self, on: bool) -> None:
        self.ring_light_on = on
        log.debug("mock_lighting.ring_light", on=on)
