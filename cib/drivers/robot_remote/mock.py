"""
Mock Epson RC+ Remote Ethernet driver — for development on laptop.

Tracks just enough state (logged in, motors on, program running, fault
flags) to let the orchestrator's startup and recovery logic exercise
realistic sequences without a real controller.
"""

from __future__ import annotations

import asyncio

import structlog

from cib.drivers.robot_remote.driver import RemoteStatus

log = structlog.get_logger(__name__)


class MockRemoteEthernetDriver:

    def __init__(self) -> None:
        self._connected = False
        self._motors_on = False
        self._running = False
        self._fault = False   # set True to simulate an EStop/Error condition in tests

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        self._connected = True
        log.info("mock_robot_remote.connected")

    async def disconnect(self) -> None:
        self._connected = False
        log.info("mock_robot_remote.disconnected")

    async def get_status(self) -> RemoteStatus:
        return RemoteStatus(
            test=False, teach=False, auto=True, warning=False, serror=False,
            safeguard=self._fault, estop=self._fault, error=self._fault,
            paused=False, running=self._running,
            ready=not self._fault,
            error_code="0517" if self._fault else "0000",
        )

    async def wait_until_ready(self, timeout_s: float = 30.0, poll_interval_s: float = 0.2) -> bool:
        return True

    async def reset(self, robot_number: int = 0) -> None:
        self._fault = False
        log.info("mock_robot_remote.reset", robot_number=robot_number)

    async def set_motors_on(self, robot_number: int = 0) -> None:
        self._motors_on = True
        log.info("mock_robot_remote.motors_on", robot_number=robot_number)

    async def set_motors_off(self, robot_number: int = 0) -> None:
        self._motors_on = False
        log.info("mock_robot_remote.motors_off", robot_number=robot_number)

    async def start(self, function_no: int = 0) -> None:
        self._running = True
        log.info("mock_robot_remote.start", function_no=function_no)

    async def stop(self, robot_number: int = 0) -> None:
        self._running = False
        log.info("mock_robot_remote.stop", robot_number=robot_number)

    async def home(self) -> None:
        log.info("mock_robot_remote.home")
