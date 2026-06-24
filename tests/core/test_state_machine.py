"""Tests for SystemStateMachine — verifies all legal and illegal transitions."""

from __future__ import annotations

import pytest
from transitions import MachineError

from cib.core.state_machine import State, SystemStateMachine


@pytest.fixture
def sm() -> SystemStateMachine:
    return SystemStateMachine()


# ── Initial state ──────────────────────────────────────────────────────────────

def test_initial_state(sm: SystemStateMachine) -> None:
    assert sm.current == State.INITIALIZING


# ── Startup ────────────────────────────────────────────────────────────────────

def test_startup_to_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    assert sm.current == State.IDLE


def test_cannot_start_from_initializing(sm: SystemStateMachine) -> None:
    with pytest.raises(MachineError):
        sm.start()


# ── Normal run lifecycle ───────────────────────────────────────────────────────

def test_idle_to_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    assert sm.current == State.RUNNING


def test_running_to_cycle_stopping(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    assert sm.current == State.CYCLE_STOPPING


def test_cycle_stopping_to_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    sm.cycle_complete()
    assert sm.current == State.IDLE


def test_resume_from_cycle_stopping(sm: SystemStateMachine) -> None:
    """Operator can restart before cycle_complete — goes back to RUNNING."""
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    sm.start()
    assert sm.current == State.RUNNING


# ── Abort ──────────────────────────────────────────────────────────────────────

def test_abort_from_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.abort()
    assert sm.current == State.IDLE


def test_abort_from_cycle_stopping(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    sm.abort()
    assert sm.current == State.IDLE


def test_cannot_abort_from_running(sm: SystemStateMachine) -> None:
    """RUNNING → abort is not a defined transition; must cycle_stop first."""
    sm.all_devices_ready()
    sm.start()
    with pytest.raises(MachineError):
        sm.abort()


# ── E-Stop ─────────────────────────────────────────────────────────────────────

def test_estop_from_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.safety_fault_estop()
    assert sm.current == State.E_STOP


def test_estop_from_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.safety_fault_estop()
    assert sm.current == State.E_STOP


def test_estop_from_cycle_stopping(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    sm.safety_fault_estop()
    assert sm.current == State.E_STOP


def test_estop_reset_to_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.safety_fault_estop()
    sm.hardware_reset_complete()
    assert sm.current == State.IDLE


def test_cannot_start_from_estop_directly(sm: SystemStateMachine) -> None:
    """E-STOP requires hardware_reset_complete, not just start."""
    sm.all_devices_ready()
    sm.start()
    sm.safety_fault_estop()
    with pytest.raises(MachineError):
        sm.start()


# ── Protective Stop ────────────────────────────────────────────────────────────

def test_protective_stop_from_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.safety_fault_protective()
    assert sm.current == State.PROTECTIVE_STOP


def test_protective_stop_from_cycle_stopping(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    sm.safety_fault_protective()
    assert sm.current == State.PROTECTIVE_STOP


def test_protective_stop_resumes_with_start(sm: SystemStateMachine) -> None:
    """Light curtain cleared — operator presses START to resume."""
    sm.all_devices_ready()
    sm.start()
    sm.safety_fault_protective()
    sm.start()
    assert sm.current == State.RUNNING


def test_cannot_protective_stop_from_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    with pytest.raises(MachineError):
        sm.safety_fault_protective()


# ── Fault (unrecoverable) ──────────────────────────────────────────────────────

def test_fault_from_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.fault()
    assert sm.current == State.FAULT


def test_fault_from_any_state(sm: SystemStateMachine) -> None:
    """Fault is catchall — can be triggered from every state."""
    for state in [State.INITIALIZING, State.IDLE, State.E_STOP]:
        fresh_sm = SystemStateMachine()
        if state == State.IDLE:
            fresh_sm.all_devices_ready()
        elif state == State.E_STOP:
            fresh_sm.all_devices_ready()
            fresh_sm.start()
            fresh_sm.safety_fault_estop()
        fresh_sm.fault()
        assert fresh_sm.current == State.FAULT


def test_fault_cleared_to_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.fault()
    sm.fault_cleared()
    assert sm.current == State.IDLE


# ── Guard helpers ──────────────────────────────────────────────────────────────

def test_is_running_true(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    assert sm.is_running() is True


def test_is_running_false(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    assert sm.is_running() is False


def test_is_safe_to_start_true(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    assert sm.is_safe_to_start() is True


def test_is_safe_to_start_false_when_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    assert sm.is_safe_to_start() is False


def test_allows_motion_running(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    assert sm.allows_motion() is True


def test_allows_motion_cycle_stopping(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    sm.start()
    sm.cycle_stop()
    assert sm.allows_motion() is True


def test_allows_motion_idle(sm: SystemStateMachine) -> None:
    sm.all_devices_ready()
    assert sm.allows_motion() is False


# ── Callback ───────────────────────────────────────────────────────────────────

def test_on_state_change_callback_fired(sm: SystemStateMachine) -> None:
    """Callback receives the new State enum on every transition."""
    received: list[State] = []

    sm2 = SystemStateMachine(on_state_change=received.append)
    sm2.all_devices_ready()
    sm2.start()
    sm2.cycle_stop()

    assert received == [State.IDLE, State.RUNNING, State.CYCLE_STOPPING]


def test_no_callback_does_not_raise(sm: SystemStateMachine) -> None:
    """No callback set — transitions still work silently."""
    sm.all_devices_ready()
    sm.start()
    assert sm.current == State.RUNNING
