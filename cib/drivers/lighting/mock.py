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
        self.channels_on: set[int] = set()

    async def connect(self) -> None:
        await asyncio.sleep(0.02)
        self._connected = True
        log.info("mock_lighting.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def set_backlight(self, on: bool) -> None:
        self.backlight_on = on
        self.channels_on = {1, 2, 3} if on else set()
        log.debug("mock_lighting.backlight", on=on)

    async def set_ring_light(self, on: bool) -> None:
        self.ring_light_on = on
        log.debug("mock_lighting.ring_light", on=on)

    async def set_backlight_channels(self, channels: list[int], on: bool) -> None:
        if on:
            self.channels_on.update(channels)
        else:
            self.channels_on.difference_update(channels)
        self.backlight_on = (self.channels_on == {1, 2, 3})
        log.debug("mock_lighting.backlight_channels", channels=channels, on=on,
                  all_channels_on=self.backlight_on)
