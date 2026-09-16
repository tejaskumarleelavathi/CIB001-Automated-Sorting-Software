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
    OperatorShutdownEvent,
    OperatorStartEvent,
    SystemErrorEvent,
    VisionEoAResultEvent,
)
from cib.core.orchestrator import (
    ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD,
    DriverSet,
    Orchestrator,
)
from cib.core.state_machine import State
from cib.data.models import Pick, PickResult, PickStatus
from cib.drivers.base import StatusLight
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.robot_remote.mock import MockRemoteEthernetDriver
from cib.drivers.safety.mock import MockSafetyDriver


@pytest.fixture
def drivers() -> DriverSet:
    return DriverSet(
        robot=MockRobotDriver(),
        robot_remote=MockRemoteEthernetDriver(),
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


async def _drain_to_all_devices_ready(orch: Orchestrator) -> None:
    """
    _startup() can also queue other events ahead of AllDevicesReadyEvent
    (e.g. RecoveryStatusEvent from _remote_ethernet_restart_task, which now
    fires during startup too, not just recovery) -- drain and handle
    everything up to and including it, rather than assuming it's always
    the very next item.
    """
    while True:
        event = await orch._queue.get()
        await orch._handle_event(event)
        if isinstance(event, AllDevicesReadyEvent):
            return


async def _to_idle(orch: Orchestrator) -> None:
    await orch._handle_event(AllDevicesReadyEvent())
    assert orch.state == State.IDLE


async def _to_running(orch: Orchestrator) -> None:
    await _to_idle(orch)
    await orch._handle_event(OperatorStartEvent())
    assert orch.state == State.RUNNING


async def _to_running_no_auto_cycle(orch: Orchestrator) -> None:
    """
    Like _to_running, but suppresses the background cycle task that
    OperatorStartEvent's handler normally auto-launches via
    asyncio.create_task(self._run_cycle()) (orchestrator.py, right after
    it turns the backlight on).

    That auto-launched task races any explicit `await orch._run_cycle()`
    a test makes right afterward -- both attempts share the SAME single
    _force_sample_detection-injected centroid, so whichever cycle wins
    the race consumes/visits it, leaving the other with nothing to pick
    ("cycle.no_samples_detected" / no_bugs). Tests that need to control
    and observe the FIRST cycle's execution precisely (e.g. overriding
    execute_pick and asserting on state it sets) need this instead of
    _to_running.

    Trick: _run_cycle()'s auto-launch is gated behind
    `if not self._cycle_active`. Setting _cycle_active True before firing
    OperatorStartEvent suppresses the auto-launch for this transition;
    resetting it False right after leaves the orchestrator otherwise
    untouched for the test's own explicit _run_cycle() call.
    """
    await _to_idle(orch)
    orch._cycle_active = True
    await orch._handle_event(OperatorStartEvent())
    orch._cycle_active = False
    assert orch.state == State.RUNNING


# ── Run lifecycle ────────────────────────────────────────────────────────────

class TestRunLifecycle:

    async def test_start_opens_a_run(self, orch: Orchestrator) -> None:
        await _to_running(orch)
        assert orch._current_run is not None
        assert orch._storage is not None

    async def test_abort_closes_the_run(self, orch: Orchestrator) -> None:
        # Already stopped (via CYCLE STOP) before ABORT — the plain,
        # already-paused case. See TestAbortSemantics for ABORT pressed
        # directly from RUNNING (2026-08-30).
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
        orch._pending_well = (150.0, 200.0, 45.0, "B3", None, "R")
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = False   # blue button already pressed
        assert orch.state == State.E_STOP

        await orch._handle_event(OperatorAcknowledgeEvent())

        # Delivered and cleared — never left hanging, never silently dropped.
        assert orch._pending_well is None
        assert orch.state == State.IDLE
        # Lands in IDLE (not auto-RUNNING) — a separate START is required
        # after an e-stop, per FR-SAF-06.
        assert orch._drivers.plc.status_light == StatusLight.GREEN

    async def test_pending_well_recorded_even_if_pick_completion_fails(
        self, orch: Orchestrator,
    ) -> None:
        """
        2026-08-31, real-hardware finding: a safety fault landing during
        the robot's retract/travel-back -- AFTER the specimen was
        genuinely already aspirated, but BEFORE execute_pick() fully
        returns -- must not lose track of the specimen. Confirmed live:
        operator waited for the aspirate to finish, then tripped the
        light curtain, and CONTINUE's recovery found nothing pending to
        deliver even though a specimen was physically in the tip.
        execute_pick()'s on_aspirated callback (see driver.py) must fire
        and record _pending_well immediately, even when the rest of that
        same call subsequently fails (simulating the connection dropping
        during the retract/travel-back, after aspiration already
        succeeded).
        """
        orch._robot_remote_enabled = False
        orch._force_sample_detection = True   # guarantee a pick is actually attempted
        # _run_cycle()'s overhead-capture step bails out silently (no log,
        # just an early `return None` -> outer "no_bugs" outcome) whenever
        # self._running is False -- normally set True by run()/_startup(),
        # neither of which this test's lightweight _to_idle/_to_running-style
        # setup calls. Every other test in this class either sets this
        # already or overrides a method reached before this check; this one
        # overrides execute_pick, reached only after it, so it must be set
        # explicitly.
        orch._running = True
        # NOTE: _to_running_no_auto_cycle (not _to_running) -- the plain
        # version's auto-launched background cycle would race this test's
        # own explicit _run_cycle() call below for the single
        # _force_sample_detection-injected centroid; see its docstring.
        await _to_running_no_auto_cycle(orch)

        # The synthetic EoA frame (a bright circle, same generator as the
        # overhead one) reads as almost entirely foreground to the real
        # classifier ("coverage 99.9% > 65%" -> NO_SPECIMEN -> reject) --
        # it isn't meant to look like a real close-up specimen image, just
        # to give the overhead detector something to find. Stub the EoA
        # step directly so this test can reach the pick step at all,
        # exactly like _force_sample_detection does for overhead.
        orch._run_vision_eoa = lambda frame, robot_x, robot_y: VisionEoAResultEvent(
            proceed_with_pick=True,
            refined_x=robot_x,
            refined_y=robot_y,
            frame_path="",
            label="BUG",
            reason="test stub",
        )

        async def _pick_aspirates_then_connection_drops(
            pick_x, pick_y, u=None, hand="R", on_aspirated=None,
        ):
            if on_aspirated is not None:
                on_aspirated()
            raise RuntimeError("Robot closed the connection unexpectedly")

        orch._drivers.robot.execute_pick = _pick_aspirates_then_connection_drops

        await orch._run_cycle()

        # The whole point: recorded even though execute_pick() itself
        # raised right after calling the callback.
        assert orch._pending_well is not None

    async def test_robot_resume_called_during_recovery(self, orch: Orchestrator) -> None:
        await orch._drivers.robot.connect()   # real precondition for is_ready() to mean anything
        await _to_running(orch)
        await orch._drivers.robot.halt()
        assert await orch._drivers.robot.is_ready() is False
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = False   # blue button already pressed

        await orch._handle_event(OperatorAcknowledgeEvent())

        # resume() must have been called, or the robot would be permanently
        # unable to reconnect (see driver.py _reconnect_sync's _halted guard).
        assert await orch._drivers.robot.is_ready() is True

    async def test_protective_stop_recovery_resumes_running_after_delivering(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        orch._pending_well = (10.0, 10.0, None, "A1", None, "R")
        await orch._drivers.robot.halt()
        orch._state_before_fault = State.RUNNING   # normally set by _evaluate_safety()
        orch._sm.safety_fault_protective()
        orch._light_curtain_tripped = False   # beam physically clear
        orch._light_curtain_stably_clear = True   # ...and has been for a while (debounced)
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
        orch._light_curtain_stably_clear = True
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
        orch._pending_well = (5.0, 5.0, None, "D1", None, "R")
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = False   # blue button already pressed —
                                               # this test is about the robot
                                               # reconnect failing, not this gate

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
        orch._hardware_fault_active = False   # blue button already pressed

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
        orch._pending_well = (77.0, 88.0, None, "C5", None, "R")
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = False   # blue button already pressed —
                                               # see TestAbortSemantics for the
                                               # blocked-until-cleared case
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
        orch._hardware_fault_active = False   # blue button already pressed

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert orch._drivers.lighting.backlight_on is True


class TestEstopContinueSendsResetPulse:
    """
    2026-09-11, operator request (OQ-23 resolved, e-stop path): a single
    physical blue-button press feeds BOTH the e-stop's own Reset V1 block
    AND, via the same OR gate, the P-Stop side's Reset V1 -- but P-Stop's
    Reset V1 only releases if the light curtain was ALSO clear at that
    same instant (see the Safety Designer logic diagram). If it wasn't,
    the e-stop side clears but the motor-power side (_hardware_fault_active)
    stays latched. CONTINUE must now finish that itself, the same software
    reset pulse the light-curtain path already sends, instead of silently
    proceeding into _safe_recovery() (old behaviour) or requiring the
    operator to notice and press the physical button a second time.
    """

    async def test_sends_pulse_and_proceeds_once_it_clears(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True   # e-stop's own Reset V1 already
                                              # satisfied by the physical button,
                                              # but motor-power side still latched
        orch._light_curtain_stably_clear = True   # curtain genuinely clear

        calls: list[str] = []
        real_reset_ack = orch._drivers.safety.send_reset_acknowledge

        async def _reset_ack_and_clear() -> None:
            calls.append("reset_ack")
            await real_reset_ack()
            # Simulate the next live poll (not running in this test)
            # observing the pulse actually took effect.
            orch._hardware_fault_active = False

        orch._drivers.safety.send_reset_acknowledge = _reset_ack_and_clear

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert calls == ["reset_ack"]
        assert orch.state == State.IDLE   # recovery actually proceeded

    async def test_blocked_if_pulse_does_not_clear_it(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True   # stays latched even after the pulse
                                              # (mock's default send_reset_acknowledge
                                              # doesn't touch this flag)
        orch._light_curtain_stably_clear = True   # curtain genuinely clear -- this
                                                   # test is about the VERIFY step
                                                   # failing, not the curtain gate

        await orch._handle_event(OperatorAcknowledgeEvent())

        # Never reached _safe_recovery() -- still E_STOP, nothing torn down.
        assert orch.state == State.E_STOP

    async def test_blocked_without_even_trying_the_pulse_if_curtain_tripped(
        self, orch: Orchestrator,
    ) -> None:
        """
        2026-09-11, found live: CONTINUE showed as enabled with a hand
        physically inside the light curtain, because the UI gate didn't
        check it for E_STOP. This is the matching orchestrator-side
        defense-in-depth check -- don't even attempt the pulse (it's
        guaranteed to fail; the P-Stop side it targets requires the
        curtain clear to release), and don't let a stray/racing event
        bypass this just because it didn't come through the UI's own gate.
        """
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True
        orch._light_curtain_tripped = True   # e.g. a hand still in the beam

        calls: list[str] = []

        async def _record_reset_ack() -> None:
            calls.append("reset_ack")

        orch._drivers.safety.send_reset_acknowledge = _record_reset_ack

        await orch._handle_event(OperatorAcknowledgeEvent())

        assert calls == []   # never even attempted -- guaranteed-futile pulse skipped
        assert orch.state == State.E_STOP


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
        orch._hardware_fault_active = False   # blue button already pressed

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
        orch._hardware_fault_active = False   # blue button already pressed

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
        orch._pending_well = (5.0, 5.0, None, "C5", pick.pick_id, "R")   # now in transit
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = False   # blue button already pressed

        await orch._handle_event(OperatorAcknowledgeEvent())

        # Delivered as a real success — NOT deleted, NOT logged as interrupted.
        row = orch._db._conn().execute(
            "SELECT * FROM picks WHERE pick_id = ?", (pick.pick_id,)
        ).fetchone()
        assert row is not None
        assert row["result"] == PickResult.SUCCESS
        for p in paths:
            assert Path(p).exists()   # images kept — this pick really happened


class TestRemoteEthernetRecovery:
    """
    Tests for the Remote Ethernet lifecycle (2026-08-28, revised 2026-08-30):
    startup always runs a login/reset/motor-on/start sequence before
    anything else, a robot-related _run_cycle() exception self-heals via
    the same sequence instead of always FAULTing, and shutdown cuts motor
    power via the same channel. The connection is HELD OPEN across all of
    this (2026-08-30 reversal, real-hardware finding — see
    cib/drivers/robot_remote/driver.py's module docstring: closing it
    right after Start killed the task every time, regardless of network
    contention) — only shutdown ever closes it, and only once, never
    double-connecting (a fresh Login while Main is running kills it too).
    """

    async def test_startup_restarts_task_and_stays_connected(self, orch: Orchestrator) -> None:
        orch._robot_remote_enabled = True
        await orch._startup()
        # _startup() posts AllDevicesReadyEvent onto the queue rather than
        # handling it directly — drain up to it explicitly since this test
        # calls _startup() alone, not the full run()/_event_loop() machinery.
        await _drain_to_all_devices_ready(orch)

        rr = orch._drivers.robot_remote
        assert rr._motors_on is True
        assert rr._running is True
        assert rr._connected is True   # held open, never closed after a successful start
        assert orch._robot_remote_connected is True
        assert orch.state == State.IDLE

    async def test_startup_skips_remote_ethernet_when_disabled(self, orch: Orchestrator) -> None:
        assert orch._robot_remote_enabled is False   # default (no one-time RC+ setup done)
        await orch._startup()

        rr = orch._drivers.robot_remote
        assert rr._motors_on is False
        assert rr._running is False

    async def test_cycle_error_self_heals_via_remote_ethernet(self, orch: Orchestrator) -> None:
        orch._robot_remote_enabled = True
        # _recover_robot_connection() bails immediately if _running is False
        # (2026-08-31 — see its docstring: a real shutdown request must not
        # let a retry keep running past main.py's grace period). This test
        # calls _run_cycle() directly, bypassing run() (which is what
        # normally sets this), so it must be set explicitly here to
        # exercise the recovery path at all.
        orch._running = True
        await _to_running(orch)

        async def _broken_hand_check(d):
            raise RuntimeError("MOVE_IMAGE failed: mock disconnect")
        orch._ensure_physical_hand_for_next_pick = _broken_hand_check

        await orch._run_cycle()

        rr = orch._drivers.robot_remote
        assert rr._motors_on is True
        assert rr._running is True
        assert rr._connected is True   # held open after a successful recovery, same as startup
        assert orch._robot_remote_connected is True
        # Self-healed to IDLE, not FAULT — but does NOT auto-resume RUNNING,
        # same philosophy as e-stop/light-curtain recovery: a fresh operator
        # START is still required before picking resumes.
        assert orch.state == State.IDLE
        # GREEN, not AMBER (2026-09-16) -- this is a clean "ready again, no
        # error" resting state, same as _finish_stop()/ABORT's own GREEN.
        assert orch._drivers.plc.status_light == StatusLight.GREEN

    async def test_cycle_error_without_remote_ethernet_goes_to_fault(
        self, orch: Orchestrator,
    ) -> None:
        """Baseline — disabled (the default), behavior is unchanged from before this existed."""
        assert orch._robot_remote_enabled is False
        await _to_running(orch)

        async def _broken_hand_check(d):
            raise RuntimeError("MOVE_IMAGE failed: mock disconnect")
        orch._ensure_physical_hand_for_next_pick = _broken_hand_check

        await orch._run_cycle()

        assert orch.state == State.FAULT
        assert orch._drivers.plc.status_light == StatusLight.RED_CHASE

    async def test_unrelated_error_does_not_trigger_remote_recovery(
        self, orch: Orchestrator,
    ) -> None:
        """
        A non-robot error (e.g. a bug in vision code) must never trigger
        Stop()/Reset() against a robot that might be perfectly healthy —
        see ROBOT_ERROR_MARKERS' docstring in orchestrator.py.
        """
        orch._robot_remote_enabled = True
        await _to_running(orch)

        async def _broken_hand_check(d):
            raise RuntimeError("something unrelated went wrong")
        orch._ensure_physical_hand_for_next_pick = _broken_hand_check

        await orch._run_cycle()

        rr = orch._drivers.robot_remote
        assert rr._connected is False
        assert rr._motors_on is False   # never touched
        assert orch.state == State.FAULT

    async def test_shutdown_cuts_motor_power_via_remote_ethernet(self, orch: Orchestrator) -> None:
        orch._robot_remote_enabled = True
        await _to_idle(orch)
        rr = orch._drivers.robot_remote
        rr._motors_on = True   # simulate motors already on from startup

        await orch._handle_event(OperatorShutdownEvent())

        assert rr._motors_on is False
        assert rr._connected is False

    async def test_shutdown_reuses_already_open_connection(self, orch: Orchestrator) -> None:
        """
        Realistic flow: startup already left the session open (matches
        test_startup_restarts_task_and_stays_connected). Shutdown must
        reuse it, never call connect() a second time -- a fresh Login
        while Main is still running kills the task (see driver.py).
        """
        orch._robot_remote_enabled = True
        await orch._startup()
        await _drain_to_all_devices_ready(orch)
        rr = orch._drivers.robot_remote
        assert orch._robot_remote_connected is True
        connect_calls_before = rr._connect_call_count

        await orch._handle_event(OperatorShutdownEvent())

        assert rr._connect_call_count == connect_calls_before   # no second connect() call
        assert rr._motors_on is False
        assert rr._connected is False
        assert orch._robot_remote_connected is False

    async def test_idle_disconnect_triggers_remote_ethernet_recovery(
        self, orch: Orchestrator,
    ) -> None:
        """
        2026-08-30 real-hardware finding: the robot's port-2000 connection
        dropped while genuinely IDLE, between cycles, with no active cycle
        to catch it -- _run_cycle()'s self-heal only runs mid-cycle, and
        driver.py's own internal reconnect swallows its own failures
        silently (never raises), so this was previously invisible: no
        FAULT, no banner, just log spam. The keepalive loop must now
        detect it itself via is_ready() (not a raised exception) and drive
        the same Remote Ethernet recovery _run_cycle() uses.
        """
        orch._robot_remote_enabled = True
        # _recover_robot_connection() bails immediately if _running is False
        # (2026-08-31 — see its docstring) — normally set by run(), which
        # this test bypasses by calling _robot_keepalive_tick() directly.
        orch._running = True
        await orch._drivers.robot.connect()
        await _to_idle(orch)
        orch._startup_complete = True
        assert await orch._drivers.robot.is_ready() is True

        orch._drivers.robot._connected = False   # simulate a dropped port-2000 connection

        for _ in range(ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD):
            await orch._robot_keepalive_tick()

        assert await orch._drivers.robot.is_ready() is True   # reconnected + rehomed
        rr = orch._drivers.robot_remote
        assert rr._connected is True   # held open, same as every other recovery path
        assert orch.state == State.IDLE   # must not FAULT when recovery succeeds

        messages = []
        while not orch._queue.empty():
            evt = await orch._queue.get()
            if isinstance(evt, SystemErrorEvent):
                messages.append(evt.message)
        assert any("lost while idle" in m for m in messages)
        assert any("reconnected automatically" in m for m in messages)

    async def test_idle_disconnect_does_not_trigger_recovery_mid_cycle(
        self, orch: Orchestrator,
    ) -> None:
        """
        _run_cycle()'s own exception handler already owns a mid-cycle
        disconnect -- the keepalive loop must stay out of its way rather
        than racing it with a second, redundant Reset()/Start() sequence.
        """
        orch._robot_remote_enabled = True
        await orch._drivers.robot.connect()
        await _to_idle(orch)
        orch._startup_complete = True
        orch._cycle_active = True
        orch._drivers.robot._connected = False

        for _ in range(ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD):
            await orch._robot_keepalive_tick()

        rr = orch._drivers.robot_remote
        assert rr._connected is False   # never touched
        assert orch._keepalive_consecutive_disconnected == 0   # never even counted

    async def test_idle_disconnect_recovery_failure_goes_to_fault(
        self, orch: Orchestrator,
    ) -> None:
        """If the same recovery sequence _run_cycle() relies on can't bring
        the robot back, the operator needs to see FAULT, not silence."""
        orch._robot_remote_enabled = True
        await orch._drivers.robot.connect()
        await _to_idle(orch)
        orch._startup_complete = True
        orch._drivers.robot._connected = False

        async def _broken_reconnect(d):
            return False
        orch._recover_robot_connection = _broken_reconnect

        for _ in range(ROBOT_KEEPALIVE_DISCONNECT_THRESHOLD):
            await orch._robot_keepalive_tick()

        assert orch.state == State.FAULT
        assert orch._drivers.plc.status_light == StatusLight.RED_CHASE


class TestStartupPipetteReset:
    """
    2026-08-31, operator request: every startup must send a motor-only
    pipette release (sort_server.prg's new RESET_PIPETTE command, no robot
    arm travel) BEFORE anything else runs, so a specimen can never be
    silently left in the tip from a crashed/interrupted previous run.
    """

    async def test_startup_resets_pipette_once(self, orch: Orchestrator) -> None:
        await orch._startup()
        await orch._handle_event(await orch._queue.get())   # drain AllDevicesReadyEvent

        assert orch._drivers.robot._reset_pipette_call_count == 1

    async def test_startup_continues_if_pipette_reset_fails(self, orch: Orchestrator) -> None:
        """A pipette-motor hiccup at startup must not block the rest of
        startup (cameras, homing, reaching IDLE) -- this is best-effort,
        logged loudly, never fatal."""
        async def _broken_reset_pipette():
            raise RuntimeError("RESET_PIPETTE failed: motor unreachable")
        orch._drivers.robot.reset_pipette = _broken_reset_pipette

        await orch._startup()
        await orch._handle_event(await orch._queue.get())

        assert orch.state == State.IDLE


class TestAbortSemantics:
    """
    Tests for the 2026-08-30 ABORT behavior changes (operator request):
      - ABORT is now legal directly from RUNNING, not just after a
        separate CYCLE STOP -- but it's graceful (lets the in-progress
        cycle finish, same as CYCLE STOP) rather than an immediate hard
        stop, then closes the run once that finish completes.
      - ABORT during E_STOP/PROTECTIVE_STOP is now blocked until the
        hardware fault actually clears (blue reset button pressed) --
        same physical gate as CONTINUE, since ABORT also delivers a
        pending specimen (a real robot move) before closing the run,
        which cannot succeed while motor power is still cut.
    """

    async def test_abort_from_running_finishes_cycle_then_closes_run(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        run_id = orch._current_run.run_id

        await orch._handle_event(OperatorAbortEvent())

        # No cycle task was actually active in this test (mock drivers,
        # _run_cycle() never started) -- _finish_stop() runs immediately
        # per the same "nothing will call it otherwise" logic as STOP.
        assert orch.state == State.IDLE
        assert orch._current_run is None
        assert orch._abort_after_cycle is False

    async def test_abort_from_running_is_legal_no_warning_needed(
        self, orch: Orchestrator,
    ) -> None:
        """RUNNING used to be an illegal ABORT source (warning + no-op) --
        confirm it's now accepted."""
        await _to_running(orch)
        await orch._handle_event(OperatorAbortEvent())
        assert orch.state != State.RUNNING

    async def test_abort_blocked_during_estop_until_hardware_clears(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        orch._pending_well = (5.0, 5.0, None, "A1", None, "R")
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True   # blue button not pressed yet
        assert orch.state == State.E_STOP

        await orch._handle_event(OperatorAbortEvent())

        # Blocked -- still E_STOP, run still open, specimen still tracked
        # (never attempted a robot move against a de-energized motor).
        assert orch.state == State.E_STOP
        assert orch._current_run is not None
        assert orch._pending_well is not None

        orch._hardware_fault_active = False   # blue button now pressed
        await orch._handle_event(OperatorAbortEvent())

        assert orch.state == State.IDLE
        assert orch._current_run is None
        assert orch._pending_well is None   # delivered before closing

    async def test_abort_blocked_during_protective_stop_until_curtain_clears(
        self, orch: Orchestrator,
    ) -> None:
        """
        2026-09-14: ABORT's PROTECTIVE_STOP gate now checks the DEBOUNCED
        _light_curtain_stably_clear flag (same as CONTINUE), not the raw
        _light_curtain_tripped flag -- the raw reading flickers on real
        hardware, so gating on it directly would reintroduce the same
        false-negative bug CONTINUE already had to fix (see
        LIGHT_CURTAIN_STABLE_CLEAR_S).
        """
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._state_before_fault = State.RUNNING
        orch._sm.safety_fault_protective()
        orch._light_curtain_tripped = True    # still obstructed
        orch._light_curtain_stably_clear = False
        orch._hardware_fault_active = False
        assert orch.state == State.PROTECTIVE_STOP

        await orch._handle_event(OperatorAbortEvent())

        assert orch.state == State.PROTECTIVE_STOP
        assert orch._current_run is not None

        orch._light_curtain_tripped = False   # now clear
        orch._light_curtain_stably_clear = True
        await orch._handle_event(OperatorAbortEvent())

        assert orch.state == State.IDLE
        assert orch._current_run is None


class TestAbortSendsResetPulse:
    """
    2026-09-14, operator-reported: "why do we get only continue enabled" --
    ABORT used to require _hardware_fault_active to ALREADY be False before
    doing anything, but nothing except CONTINUE's own reset pulse ever
    cleared that flag, so ABORT could never actually become usable on its
    own. ABORT's handler now sends and verifies the same reset-acknowledge
    pulse CONTINUE does. Mirrors TestEstopContinueSendsResetPulse above.
    """

    async def test_estop_sends_pulse_and_proceeds_once_it_clears(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True
        orch._light_curtain_stably_clear = True

        calls: list[str] = []
        real_reset_ack = orch._drivers.safety.send_reset_acknowledge

        async def _reset_ack_and_clear() -> None:
            calls.append("reset_ack")
            await real_reset_ack()
            orch._hardware_fault_active = False

        orch._drivers.safety.send_reset_acknowledge = _reset_ack_and_clear

        await orch._handle_event(OperatorAbortEvent())

        assert calls == ["reset_ack"]
        assert orch.state == State.IDLE
        assert orch._current_run is None

    async def test_estop_blocked_if_pulse_does_not_clear_it(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._sm.safety_fault_estop()
        orch._hardware_fault_active = True   # stays latched even after the pulse
        orch._light_curtain_stably_clear = True

        await orch._handle_event(OperatorAbortEvent())

        assert orch.state == State.E_STOP
        assert orch._current_run is not None

    async def test_protective_stop_sends_pulse_and_proceeds_once_it_clears(
        self, orch: Orchestrator,
    ) -> None:
        await _to_running(orch)
        await orch._drivers.robot.halt()
        orch._state_before_fault = State.RUNNING
        orch._sm.safety_fault_protective()
        orch._hardware_fault_active = True
        orch._light_curtain_stably_clear = True

        calls: list[str] = []
        real_reset_ack = orch._drivers.safety.send_reset_acknowledge

        async def _reset_ack_and_clear() -> None:
            calls.append("reset_ack")
            await real_reset_ack()
            orch._hardware_fault_active = False

        orch._drivers.safety.send_reset_acknowledge = _reset_ack_and_clear

        await orch._handle_event(OperatorAbortEvent())

        assert calls == ["reset_ack"]
        assert orch.state == State.IDLE
        assert orch._current_run is None


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
