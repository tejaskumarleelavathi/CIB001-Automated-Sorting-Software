"""Tests for all mock hardware drivers."""

from __future__ import annotations

import pytest

from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.safety.mock import MockSafetyDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver


# ── Robot ──────────────────────────────────────────────────────────────────────

class TestMockRobotDriver:

    @pytest.fixture
    async def robot(self) -> MockRobotDriver:
        drv = MockRobotDriver()
        await drv.connect()
        return drv

    async def test_connect_sets_ready(self, robot: MockRobotDriver) -> None:
        assert await robot.is_ready() is True

    async def test_disconnected_not_ready(self) -> None:
        drv = MockRobotDriver()
        assert await drv.is_ready() is False

    async def test_halt_blocks_motion(self, robot: MockRobotDriver) -> None:
        await robot.halt()
        assert await robot.is_ready() is False

    async def test_resume_restores_ready(self, robot: MockRobotDriver) -> None:
        await robot.halt()
        robot.resume()
        assert await robot.is_ready() is True

    async def test_move_to_image_site_completes(self, robot: MockRobotDriver) -> None:
        """Smoke test — should not raise."""
        await robot.move_to_image_site(100.0, 200.0)

    async def test_execute_pick_place_completes(self, robot: MockRobotDriver) -> None:
        await robot.execute_pick_place(10.0, 20.0, 100.0, 100.0)

    async def test_move_to_safe_park_completes(self, robot: MockRobotDriver) -> None:
        await robot.move_to_safe_park()


# ── Safety ─────────────────────────────────────────────────────────────────────

class TestMockSafetyDriver:

    @pytest.fixture
    async def safety(self) -> MockSafetyDriver:
        drv = MockSafetyDriver()
        await drv.connect()
        return drv

    async def test_default_status_no_fault(self, safety: MockSafetyDriver) -> None:
        status = await safety.read_status()
        assert status["any_fault_active"] is False
        assert status["estop_active"] is False
        assert status["light_curtain_tripped"] is False
        assert status["contactor_closed"] is True

    async def test_inject_estop_sets_fault(self, safety: MockSafetyDriver) -> None:
        safety.inject_estop(button_index=1)
        status = await safety.read_status()
        assert status["any_fault_active"] is True
        assert status["estop_active"] is True
        assert status["estop_button_index"] == 1
        assert status["contactor_closed"] is False

    async def test_clear_estop_available_for_reset(self, safety: MockSafetyDriver) -> None:
        safety.inject_estop()
        safety.clear_estop()
        status = await safety.read_status()
        assert status["estop_active"] is False
        assert status["reset_available"] is True

    async def test_press_reset_closes_contactor(self, safety: MockSafetyDriver) -> None:
        safety.inject_estop()
        safety.clear_estop()
        safety.press_reset()
        status = await safety.read_status()
        assert status["contactor_closed"] is True
        assert status["any_fault_active"] is False

    async def test_inject_light_curtain(self, safety: MockSafetyDriver) -> None:
        safety.inject_light_curtain_trip()
        status = await safety.read_status()
        assert status["light_curtain_tripped"] is True
        assert status["any_fault_active"] is True

    async def test_clear_light_curtain(self, safety: MockSafetyDriver) -> None:
        safety.inject_light_curtain_trip()
        safety.clear_light_curtain()
        status = await safety.read_status()
        assert status["light_curtain_tripped"] is False
        assert status["any_fault_active"] is False


# ── PLC ────────────────────────────────────────────────────────────────────────

class TestMockPLCDriver:

    @pytest.fixture
    async def plc(self) -> MockPLCDriver:
        drv = MockPLCDriver()
        await drv.connect()
        return drv

    async def test_connect_succeeds(self, plc: MockPLCDriver) -> None:
        assert plc._connected is True

    async def test_read_event_log_returns_list(self, plc: MockPLCDriver) -> None:
        entries = await plc.read_event_log()
        assert isinstance(entries, list)

    async def test_set_status_light_does_not_raise(self, plc: MockPLCDriver) -> None:
        from cib.drivers.base import StatusLight
        for light in [StatusLight.GREEN, StatusLight.AMBER, StatusLight.RED, StatusLight.OFF]:
            await plc.set_status_light(light)


# ── Pump ───────────────────────────────────────────────────────────────────────

class TestMockPumpDriver:

    @pytest.fixture
    async def pump(self) -> MockPumpDriver:
        drv = MockPumpDriver()
        await drv.connect()
        return drv

    async def test_connect_sets_ready(self, pump: MockPumpDriver) -> None:
        assert await pump.is_ready() is True

    async def test_aspirate_completes(self, pump: MockPumpDriver) -> None:
        await pump.aspirate(volume_ul=5.0, speed=1.0)

    async def test_dispense_completes(self, pump: MockPumpDriver) -> None:
        await pump.dispense(volume_ul=5.0, speed=1.0)

    async def test_not_ready_before_connect(self) -> None:
        drv = MockPumpDriver()
        assert await drv.is_ready() is False


# ── Camera ─────────────────────────────────────────────────────────────────────

class TestMockCameraDriver:

    @pytest.fixture
    async def camera(self) -> MockCameraDriver:
        drv = MockCameraDriver(name="test", num_fake_samples=3)
        await drv.connect()
        return drv

    async def test_capture_returns_numpy_array(self, camera: MockCameraDriver) -> None:
        import numpy as np
        frame = await camera.capture()
        assert isinstance(frame, np.ndarray)

    async def test_capture_correct_shape(self, camera: MockCameraDriver) -> None:
        frame = await camera.capture()
        assert frame.shape == (1024, 1280)

    async def test_synthetic_frame_has_bright_spots(self, camera: MockCameraDriver) -> None:
        """Synthetic frame must have some bright pixels so the vision pipeline detects them."""
        import numpy as np
        frame = await camera.capture()
        assert np.any(frame > 100), "Synthetic frame has no bright pixels — vision pipeline will never detect samples"

    async def test_frame_count_increments(self, camera: MockCameraDriver) -> None:
        assert camera._frame_count == 0
        await camera.capture()
        assert camera._frame_count == 1
        await camera.capture()
        assert camera._frame_count == 2


# ── Lighting ───────────────────────────────────────────────────────────────────

class TestMockLightingDriver:

    @pytest.fixture
    async def lighting(self) -> MockLightingDriver:
        drv = MockLightingDriver()
        await drv.connect()
        return drv

    async def test_initial_state_off(self, lighting: MockLightingDriver) -> None:
        assert lighting.backlight_on is False
        assert lighting.ring_light_on is False

    async def test_set_backlight_on(self, lighting: MockLightingDriver) -> None:
        await lighting.set_backlight(True)
        assert lighting.backlight_on is True

    async def test_set_backlight_off(self, lighting: MockLightingDriver) -> None:
        await lighting.set_backlight(True)
        await lighting.set_backlight(False)
        assert lighting.backlight_on is False

    async def test_set_ring_light_on(self, lighting: MockLightingDriver) -> None:
        await lighting.set_ring_light(True)
        assert lighting.ring_light_on is True
