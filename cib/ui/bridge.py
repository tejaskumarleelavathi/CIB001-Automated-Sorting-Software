"""
cib/ui/bridge.py — asyncio-thread-to-Qt-thread event bridge.

Orchestrator accepts on_state_change/on_event callbacks (cib/core/orchestrator.py
__init__) that already fire on every state transition and every SystemEvent --
on_state_change is wired directly into SystemStateMachine, on_event fires in
_event_loop() before each event is handled. Neither was ever passed by main.py,
so main_window.py has been polling orchestrator.state every 500ms instead.

Both callbacks fire on the asyncio thread (see architecture/ui.md's threading
model). Qt widgets must never be touched from that thread directly -- pyqtSignal
handles the thread hop safely (Qt queues the emission and delivers it on the
receiving object's thread, which for UiBridge is the main/Qt thread since it's
constructed there), so this class's job is just: receive the callback, emit the
signal, nothing else. Screens connect to these signals instead of polling.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from cib.core.events import SystemEvent
from cib.core.state_machine import State


class UiBridge(QObject):
    """
    Construct ONCE on the Qt/main thread, before the orchestrator's asyncio
    thread starts. Pass self.on_state_change/self.on_event as the
    Orchestrator(...) constructor's on_state_change=/on_event= kwargs.
    """

    state_changed = pyqtSignal(object)   # emits State
    event_received = pyqtSignal(object)  # emits SystemEvent (any variant)

    def on_state_change(self, state: State) -> None:
        """Passed as Orchestrator's on_state_change= -- called on the asyncio thread."""
        self.state_changed.emit(state)

    def on_event(self, event: SystemEvent) -> None:
        """Passed as Orchestrator's on_event= -- called on the asyncio thread."""
        self.event_received.emit(event)
