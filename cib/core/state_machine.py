"""
System state machine.

Single source of truth for what state the system is in and what
transitions are legal. Nothing outside this file decides what state
we're in — everything goes through here.

States (from PRD Section 11):
    INITIALIZING  → connecting to all devices on startup
    IDLE          → ready, no run active
    RUNNING       → actively picking and placing
    CYCLE_STOPPING → finishing current cycle then stopping gracefully
    E_STOP        → hardware e-stop active, full reset required
    PROTECTIVE_STOP → light curtain break (no reset needed, just START)
    FAULT         → unrecoverable software/hardware error
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Callable

from transitions import Machine

import structlog

log = structlog.get_logger(__name__)


class State(str, Enum):
    INITIALIZING    = "INITIALIZING"
    IDLE            = "IDLE"
    RUNNING         = "RUNNING"
    CYCLE_STOPPING  = "CYCLE_STOPPING"
    E_STOP          = "E_STOP"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    FAULT           = "FAULT"


class SystemStateMachine:
    """
    Wraps the transitions library state machine.

    Usage:
        sm = SystemStateMachine(on_state_change=my_callback)
        sm.all_devices_ready()   # INITIALIZING → IDLE
        sm.start()               # IDLE → RUNNING
        sm.safety_fault()        # RUNNING → E_STOP
    """

    # Transition table — (trigger, source, destination)
    _TRANSITIONS = [
        # Normal startup
        {"trigger": "all_devices_ready",  "source": "INITIALIZING",    "dest": "IDLE"},

        # Run lifecycle
        {"trigger": "start",              "source": "IDLE",             "dest": "RUNNING"},
        {"trigger": "cycle_stop",         "source": "RUNNING",          "dest": "CYCLE_STOPPING"},
        {"trigger": "cycle_complete",     "source": "CYCLE_STOPPING",   "dest": "IDLE"},
        {"trigger": "abort",              "source": ["IDLE", "CYCLE_STOPPING", "E_STOP", "PROTECTIVE_STOP", "FAULT"], "dest": "IDLE"},

        # Resume after graceful stop
        {"trigger": "start",              "source": "CYCLE_STOPPING",   "dest": "RUNNING"},

        # Safety — e-stop (full reset required)
        {"trigger": "safety_fault_estop", "source": ["RUNNING", "CYCLE_STOPPING", "IDLE"], "dest": "E_STOP"},
        {"trigger": "hardware_reset_complete", "source": ["E_STOP", "PROTECTIVE_STOP"], "dest": "IDLE"},

        # Safety — protective stop (just press start). IDLE is a legal
        # source too — found via a live crash 2026-08-18: the light curtain
        # can trip while nothing is running (someone reaches in between
        # cycles), and the orchestrator's own guard let it try this
        # transition from IDLE without checking, hitting this exact gap and
        # taking the whole backend down. hardware_reset_complete (not
        # "start") is used to recover FROM that specific case, since nothing
        # was running before the fault and nothing should start running now.
        {"trigger": "safety_fault_protective", "source": ["RUNNING", "CYCLE_STOPPING", "IDLE"], "dest": "PROTECTIVE_STOP"},
        {"trigger": "start",              "source": "PROTECTIVE_STOP",  "dest": "RUNNING"},

        # Fault (unrecoverable)
        {"trigger": "fault",              "source": "*",                 "dest": "FAULT"},
        {"trigger": "fault_cleared",      "source": "FAULT",             "dest": "IDLE"},
    ]

    def __init__(self, on_state_change: Callable[[State, State], None] | None = None) -> None:
        """
        Args:
            on_state_change: callback(old_state, new_state) fired on every transition.
                             Use this to update the UI and log.
        """
        self._on_state_change = on_state_change
        self._machine = Machine(
            model=self,
            states=[s.value for s in State],
            transitions=self._TRANSITIONS,
            initial=State.INITIALIZING.value,
            after_state_change="_after_transition",
            ignore_invalid_triggers=False,   # raise on illegal transitions — fail loud
        )

    def _after_transition(self) -> None:
        new_state = State(self.state)
        log.info("state_transition", new_state=new_state.value)
        if self._on_state_change:
            self._on_state_change(new_state)

    @property
    def current(self) -> State:
        return State(self.state)

    def is_running(self) -> bool:
        return self.current == State.RUNNING

    def is_safe_to_start(self) -> bool:
        return self.current == State.IDLE

    def allows_motion(self) -> bool:
        return self.current in {State.RUNNING, State.CYCLE_STOPPING}
