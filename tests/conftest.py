"""Shared pytest fixtures for CIB Software test suite."""

from __future__ import annotations

import asyncio
import pytest

from cib.core.state_machine import SystemStateMachine
from cib.drivers.safety.mock import MockSafetyDriver
from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver
from cib.core.orchestrator import DriverSet, Orchestrator


@pytest.fixture
def sm() -> SystemStateMachine:
    """Fresh state machine in INITIALIZING state."""
    return SystemStateMachine()


@pytest.fixture
def sm_idle() -> SystemStateMachine:
    """State machine already transitioned to IDLE (all devices ready)."""
    sm = SystemStateMachine()
    sm.all_devices_ready()
    return sm


@pytest.fixture
def mock_drivers() -> DriverSet:
    """Full set of mock drivers for orchestrator tests."""
    return DriverSet(
        robot=MockRobotDriver(),
        safety=MockSafetyDriver(),
        plc=MockPLCDriver(),
        pump=MockPumpDriver(),
        camera_overhead=MockCameraDriver(name="overhead", num_fake_samples=3),
        camera_eoa=MockCameraDriver(name="eoa", num_fake_samples=1),
        lighting=MockLightingDriver(),
    )


@pytest.fixture
def orchestrator(mock_drivers: DriverSet) -> Orchestrator:
    """Orchestrator wired with mock drivers."""
    return Orchestrator(drivers=mock_drivers)
