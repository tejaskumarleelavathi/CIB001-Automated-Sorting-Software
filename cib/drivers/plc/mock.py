"""Mock Logic PLC driver — controls status/work light and event log."""

from __future__ import annotations
import asyncio
import structlog

log = structlog.get_logger(__name__)


class MockPLCDriver:

    def __init__(self) -> None:
        self._connected = False
        self.status_light = "off"
        self.work_light = False
        self._event_log: list[str] = []

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        self._connected = True
        log.info("mock_plc.connected")

    async def disconnect(self) -> None:
        self._connected = False

    async def set_status_light(self, colour: str) -> None:
        self.status_light = colour
        log.info("mock_plc.status_light", colour=colour)

    async def set_work_light(self, on: bool) -> None:
        self.work_light = on
        log.info("mock_plc.work_light", on=on)

    async def read_event_log(self) -> list[str]:
        entries = self._event_log.copy()
        self._event_log.clear()
        return entries

    def inject_log_entry(self, entry: str) -> None:
        self._event_log.append(entry)
