"""Mock M-Drive syringe pump driver."""

from __future__ import annotations
import asyncio
import structlog

log = structlog.get_logger(__name__)

MOCK_ASPIRATE_SECONDS = 0.5
MOCK_DISPENSE_SECONDS = 0.5


class MockPumpDriver:

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        self._connected = True
        log.info("mock_pump.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def aspirate(self, volume_ul: float, speed: float) -> None:
        log.info("mock_pump.aspirate", volume_ul=volume_ul, speed=speed)
        await asyncio.sleep(MOCK_ASPIRATE_SECONDS)
        log.info("mock_pump.aspirate_complete")

    async def dispense(self, volume_ul: float, speed: float) -> None:
        log.info("mock_pump.dispense", volume_ul=volume_ul, speed=speed)
        await asyncio.sleep(MOCK_DISPENSE_SECONDS)
        log.info("mock_pump.dispense_complete")

    async def is_ready(self) -> bool:
        return self._connected
