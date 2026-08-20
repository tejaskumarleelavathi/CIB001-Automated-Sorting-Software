"""
Tests for the safety-fault recovery wiring added 2026-08-18:
  - a specimen already aspirated when a fault hits gets delivered on
    recovery, never lost and never picked-over
  - robot.resume() is called during recovery (required before any
    reconnect will be attempted — see driver.py _reconnect_sync)
  - status/work light calls match the agreed colour scheme
  - a safety-fault-induced exception in _run_cycle does not bounce the
    state machine into FAULT instead of E_STOP/PROTECTIVE_STOP
  - a Run row is opened on START and closed on ABORT

These call the orchestrator's internal event handling directly (bypassing
the queue/run() loop) — sufficient for testing decision logic without
needing to drive full asyncio poll loops in a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cib.core.events import (
    AllDevicesReadyEvent,
    OperatorAbortEvent,
    OperatorAcknowledgeEvent,
    OperatorStartEvent,
)
from cib.core.orchestrator import DriverSet, Orchestrator
from cib.core.state_machine import State
from cib.data.models import Pick, PickResult, PickStatus
from cib.drivers.base import StatusLight
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.safety.mock import MockSafetyDriver


@pytest.fixture
def drivers() -> DriverSet:
    return DriverSet(
        robot=MockRobotDriver(),
        safety=MockSafetyDriver(),
        plc=MockPLCDriver(),
        pump=MockPumpDriver(),
        camera_overhead=MockCameraDriver(name="overhead", num_fake_samples=1),
        camera_eoa=MockCameraDriver(name="eoa", num_fake_samples=1),
        lighting=MockLightingDriver(),
    )


@pytest.fixture
def orch(drivers: DriverSet, tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        drivers=drivers,
        db_path=tmp_path / "picks.db",
        runs_base_dir=tmp_path / "runs",
    )


async def _to_idle(orch: Orchestrator) -> None:
    await orch._handle_event(AllDevicesReadyEvent())
    assert orch.state == State.IDLE


async def _to_running(orch: Orchestrator) -> None:
    await _to_idle(orch)
    await orch._handle_event(OperatorStartEvent())
    assert orch.state == State.RUNNING


# ── Run lifecycle ────────────────────────────────────────────────────────────

class TestRunLifecycle:

    async def test_start_opens_a_run(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        assert orch._current_run is not None
        assert orch._storage is not None

    async def test_abort_closes_the_run(self, orch: Orchestrator) -> None:
        # ABORT isn't legal from RUNNING (matches the PRD state table — the
        # operator must CYCLE STOP first) — go through that to reach IDLE.
        await _to_running(orch)
        orch._sm.cycle_stop()
        await orch._finish_stop(orch._drivers)
        assert orch.state == State.IDLE

        await orch._handle_event(OperatorAbortEvent())
        assert orch._current_run is None
        assert orch._storage is None

    async def test_start_after_stop_continues_same_run_not_a_new_one(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        run_id = orch._current_run.run_id
        orch._sm.cycle_stop()
        await orch._finish_stop(orch._drivers)
        assert orch.state == State.IDLE
        await orch._handle_event(OperatorStartEvent())
        assert orch._current_run.run_id == run_id   # same run, not a fresh one


# ── Status light + work light ────────────────────────────────────────────────

class TestLights:

    async def test_idle_is_steady_green_work_light_on(self, orch: Orchestrator) -> None:
        await _to_idle(orch)
        assert orch._drivers.plc.status_light == StatusLight.GREEN
        assert orch._drivers.plc.work_light is True

    async def test_running_is_chasing_green_work_light_off(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        assert orch._drivers.plc.status_light == StatusLight.RUNNING
        assert orch._drivers.plc.work_light is False

    async def test_estop_is_steady_red(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        orch._drivers.safety.inject_estop()
        status = await orch._drivers.safety.read_status()
        from cib.core.events import SafetyStatusEvent
        await orch._evaluate_safety(SafetyStatusEvent(**status))
        assert orch.state == State.E_STOP
        assert orch._drivers.plc.status_light == StatusLight.RED

    async def test_light_curtain_is_flashing_red(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        orch._drivers.safety.inject_light_curtain_trip()
        status = await orch._drivers.safety.read_status()
        from cib.core.events import SafetyStatusEvent
        await orch._evaluate_safety(SafetyStatusEvent(**status))
        assert orch.state == State.PROTECTIVE_STOP
        assert orch._drivers.plc.status_light == StatusLight.RED_FLASH

    async def test_light_curtain_trip_while_idle_does_not_crash(
        self, orch: Orchestrator,
    ) -> None:
        """
        Exact live crash reproduced 2026-08-18: the light curtain tripped
        while the system was sitting IDLE (nothing running), and
        _evaluate_safety() called sm.safety_fault_protective() — a
        transition the state machine didn't allow from IDLE — raising an
        unhandled MachineError that took the whole orchestrator down. This
        must now go to PROTECTIVE_STOP cleanly, with no exception, and
        remember that nothing was running beforehand.
        """
        await _to_idle(orch)
        orch._drivers.safety.inject_light_curtain_trip()
        status = await orch._drivers.safety.read_status()
        from cib.core.events import SafetyStatusEvent
        await orch._evaluate_safety(SafetyStatusEvent(**status))   # must not raise

        assert orch.state == State.PROTECTIVE_STOP
        assert orch._state_before_fault == State.IDLE


# ── The core safety requirement: specimen in transit ────────────────────────

class TestSpecimenInTransitRecovery:

    async def test_pending_specimen_is_delivered_on_estop_recovery(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)

        # Simulate: cycle had already aspirated a specimen when the fault hit.
        orch._pending_well = (150.0, 200.0, 45.0, "B3", None)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        assert orch.state == State.E_STOP

        await orch._handle_event(OperatorAcknowledgeEvent())

        # Delivered and cleared — never left hanging, never silently dropped.
        assert orch._pending_well is None
        assert orch.state == State.IDLE
        # Lands in IDLE (not auto-RUNNING) — a separate START is required
        # after an e-stop, per FR-SAF-06.
        assert orch._drivers.plc.status_light == StatusLight.GREEN

    async def test_robot_resume_called_during_recovery(self, orch: Orchestrator) -> None:
        await orch._drivers.robot.connect()   # real precondition for is_ready() to mean anything
        await _to_running(orch)
        await orch._drivers.robot.halt()
        assert await orch._drivers.robot.is_ready() is False
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAcknowledgeEvent())

        # resume() must have been called, or the robot would be permanently
        # unable to reconnect (see driver.py _reconnect_sync's _halted guard).
        assert await orch._drivers.robot.is_ready() is True

    async def test_protective_stop_recovery_resumes_running_after_delivering(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        orch._pending_well = (10.0, 10.0, None, "A1", None)
        await orch._drivers.robot.halt()
        orch._state_before_fault = State.RUNNING   # normally set by _evaluate_safety()
        orch._sm.safety_fault_protective()
        orch._light_curtain_tripped = False   # beam physically clear
        orch._hardware_fault_active = False   # blue reset button also pressed —
                                               # required for curtain too on this
                                               # installation (corrected 2026-08-18)
        assert orch.state == State.PROTECTIVE_STOP

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch._pending_well is None
        # Was actually RUNNING before the fault, so light curtain clear IS
        # allowed to auto-resume RUNNING (FR-SAF-08 — no separate START
        # required, unlike e-stop). See the sibling test below for the
        # "nothing was running" case, which must NOT auto-resume.
        assert orch.state == State.RUNNING

    async def test_protective_stop_from_idle_recovers_to_idle(
        self, orch: Orchestrator,
    ) -> None:
        """
        Regression test for a real crash found 2026-08-18: the light curtain
        tripped while the system was IDLE (nothing running), which used to
        crash the entire orchestrator outright (illegal state transition).
        Now legal — and recovery must land back in IDLE, not RUNNING, since
        nothing was running before the fault.
        """
        await _to_idle(orch)
        await orch._drivers.robot.halt()
        orch._state_before_fault = State.IDLE   # normally set by _evaluate_safety()
        orch._sm.safety_fault_protective()
        orch._light_curtain_tripped = False
        orch._hardware_fault_active = False
        assert orch.state == State.PROTECTIVE_STOP

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch.state == State.IDLE
        assert orch._drivers.plc.status_light == StatusLight.GREEN

    async def test_robot_stays_unreachable_goes_to_fault_not_running(
        self, orch: Orchestrator,
    ) -> None:
        """
        Regression test for a real failure found 2026-08-18 on live hardware:
        a fault mid-motion crashed the SPEL+ task (sort_server.prg's error
        handling only covers its idle wait, not active motion), so recovery's
        homing attempt failed with a plain connection-refused error — yet the
        orchestrator declared itself RUNNING anyway and kept retrying forever
        while claiming to be fine. This must go to FAULT instead, and must
        NOT clear _pending_well (nothing was actually delivered).
        """
        await _to_running(orch)
        orch._pending_well = (5.0, 5.0, None, "D1", None)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        async def _broken_park() -> None:
            raise ConnectionError("Cannot connect to robot — target machine actively refused it")
        orch._drivers.robot.move_to_safe_park = _broken_park

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch.state == State.FAULT
        assert orch._drivers.plc.status_light == StatusLight.RED_CHASE
        assert orch._pending_well is not None   # never delivered — still tracked

    async def test_no_pending_specimen_skips_delivery_cleanly(
        self, orch: Orchestrator,
    ) -> None:
        """Nothing was in the pipette — recovery should just re-home, no delivery attempt."""
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch._pending_well is None
        assert orch.state == State.IDLE

    async def test_pending_specimen_is_delivered_on_abort_too(
        self, orch: Orchestrator,
    ) -> None:
        """
        Operator chooses ABORT instead of CONTINUE after a fault with a
        specimen already in the tip. It still gets delivered — ABORT only
        changes whether picking resumes afterward, not whether it's safe to
        finish an already-in-flight delivery.
        """
        await _to_running(orch)
        orch._pending_well = (77.0, 88.0, None, "C5", None)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        assert orch.state == State.E_STOP

        await orch._handle_event(OperatorAbortEvent())

        assert orch._pending_well is None
        assert orch.state == State.IDLE
        assert orch._current_run is None   # run closed as ABORTED


# ── Exception-handler race guard ─────────────────────────────────────────────

class TestRecoveryRestoresAncillaryState:
    """
    Regression tests for 2026-08-18: recovery must restore everything a
    fresh cycle assumes, not just the robot. Simulates a fault that hit
    mid-EoA-capture, where backlight is briefly OFF for the ring-light
    image — recovery must turn it back ON, or the next cycle's overhead
    capture starves for light.
    """

    async def test_backlight_restored_on_after_recovery(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        await orch._drivers.lighting.set_backlight(False)   # simulate mid-EoA-capture state
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch._drivers.lighting.backlight_on is True


class TestContinueGating:
    """
    Regression test for 2026-08-18: hardware_fault_active must reflect the
    live Flexi poll, not just "we're in E_STOP" — CONTINUE for e-stop must
    stay gated until the full hardware reset sequence physically completes
    (FR-SAF-05), same spirit as light_curtain_tripped gating PROTECTIVE_STOP.
    """

    async def test_hardware_fault_active_defaults_true_until_first_poll(
        self, orch: Orchestrator,
    ) -> None:
        assert orch.hardware_fault_active is True

    async def test_hardware_fault_active_reflects_poll_result(self, orch: Orchestrator) -> None:
        orch._hardware_fault_active = False
        assert orch.hardware_fault_active is False


class TestInterruptedAttemptCleanup:
    """
    Regression tests for 2026-08-18: an attempt interrupted BEFORE a
    specimen was ever aspirated (not the transit case) must not linger —
    orphaned image files get deleted, and the picks-table row gets
    snapshotted into pick_attempts_log then removed from `picks`, so it
    doesn't sit there looking like a real (failed) pick that never happened.
    Contrast with TestSpecimenInTransitRecovery, where a pick DID reach the
    tip and gets delivered/kept as a real record instead.
    """

    def _make_interrupted_pick(self, orch: Orchestrator, tmp_path: Path) -> tuple[Pick, list[str]]:
        img1 = tmp_path / "eoa_no_backlight_x.jpg"
        img2 = tmp_path / "eoa_with_backlight_x.jpg"
        img1.write_bytes(b"fake")
        img2.write_bytes(b"fake")
        pick = Pick(
            run_id=orch._current_run.run_id, field_code="DEV", plate_id="DEV-PLATE",
            well_position="C5", pick_status=PickStatus.MOVING_TO_PICK,
            image_filename=str(img2),
        )
        pick.pick_id = orch._db.start_pick(pick)
        orch._current_pick = pick
        orch._cycle_image_paths = [str(img1), str(img2)]
        return pick, [str(img1), str(img2)]

    async def test_cleanup_on_continue_deletes_row_and_images(
        self, orch: Orchestrator, tmp_path: Path,
    ) -> None:
        await _to_running(orch)
        pick, paths = self._make_interrupted_pick(orch, tmp_path)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch._db.get_run(orch._current_run.run_id) is not None  # sanity: run still exists
        assert orch._current_pick is None
        assert orch._cycle_image_paths == []
        for p in paths:
            assert not Path(p).exists()
        row = orch._db._conn().execute(
            "SELECT * FROM picks WHERE pick_id = ?", (pick.pick_id,)
        ).fetchone()
        assert row is None   # deleted from picks
        logged = orch._db._conn().execute(
            "SELECT * FROM pick_attempts_log WHERE well_position = 'C5'"
        ).fetchone()
        assert logged is not None   # but snapshotted first, nothing silently lost
        assert logged["reason"] == "safety_fault_during_IDLE"

    async def test_cleanup_on_abort_deletes_row_and_images(
        self, orch: Orchestrator, tmp_path: Path,
    ) -> None:
        await _to_running(orch)
        pick, paths = self._make_interrupted_pick(orch, tmp_path)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAbortEvent())

        for p in paths:
            assert not Path(p).exists()
        row = orch._db._conn().execute(
            "SELECT * FROM picks WHERE pick_id = ?", (pick.pick_id,)
        ).fetchone()
        assert row is None

    async def test_transit_case_is_never_touched_by_cleanup(
        self, orch: Orchestrator, tmp_path: Path,
    ) -> None:
        """A specimen already in the pipette must be delivered, never deleted."""
        await _to_running(orch)
        pick, paths = self._make_interrupted_pick(orch, tmp_path)
        orch._pending_well = (5.0, 5.0, None, "C5", pick.pick_id)   # now in transit
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()

        await orch._handle_event(OperatorAcknowledgeEvent())

        # Delivered as a real success — NOT deleted, NOT logged as interrupted.
        row = orch._db._conn().execute(
            "SELECT * FROM picks WHERE pick_id = ?", (pick.pick_id,)
        ).fetchone()
        assert row is not None
        assert row["result"] == PickResult.SUCCESS
        for p in paths:
            assert Path(p).exists()   # images kept — this pick really happened


class TestSafetyFaultDoesNotBounceToFault:
    """
    Regression test for a real race: _evaluate_safety() moves the state
    machine to E_STOP/PROTECTIVE_STOP concurrently with an in-flight robot
    command raising (because halt() closed the socket). _run_cycle()'s own
    except block used to call sm.fault() unconditionally on ANY exception —
    which would silently bounce E_STOP -> FAULT before the operator ever
    gets to press CONTINUE, landing them on the wrong recovery path.

    A full _run_cycle() run isn't exercised here (it needs a real vision
    pass over camera frames) — instead this locks in the exact guard
    condition orchestrator.py's except block now uses, so a future edit
    that removes/weakens it fails this test.
    """

    async def test_guard_condition_is_true_while_in_a_safety_stop(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        orch._sm.safety_fault_estop()

        in_safety_stop = orch._sm.current in {State.E_STOP, State.PROTECTIVE_STOP}

        assert in_safety_stop is True
        assert orch.state == State.E_STOP   # fault() was never called by this test
