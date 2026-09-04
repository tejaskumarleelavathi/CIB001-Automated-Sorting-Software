"""
cib/ui/main_window.py — top-level window shell (Phase 2, 2026-08-27).

Replaces the original Phase 1 placeholder (single flat window with every
control on it) with a thin QStackedWidget host swapping between two real
screens:
  - PreRunScreen  (cib/ui/screens/pre_run.py)  -- shown while IDLE/INITIALIZING
  - RunViewScreen (cib/ui/screens/run_view.py) -- shown in every other state

Screen switching is driven by the orchestrator's actual state (via
UiBridge), not guessed client-side -- so it also correctly switches back
to the pre-run screen after an ABORT returns the system to IDLE, not just
forward when a run starts.

Orchestrator API this depends on is unchanged from Phase 1
(architecture/ui.md): operator_start/stop/abort/acknowledge/shutdown,
.state. New in Phase 2: operator_configure_pre_run(), and the
on_state_change/on_event constructor hooks (see cib/ui/bridge.py) --
already-existing, previously-unused hooks in Orchestrator.__init__, wired
up for the first time here instead of the original 500ms poll.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QMainWindow, QStackedWidget
from PyQt6.QtGui import QCloseEvent

from cib.core.events import RunStatusEvent
from cib.core.state_machine import State
from cib.ui import theme
from cib.ui.bridge import UiBridge
from cib.ui.screens.pre_run import PreRunScreen
from cib.ui.screens.run_view import RunViewScreen

_IDLE_STATES = {State.INITIALIZING, State.IDLE}
# A safety fault tripped before a run was ever started (2026-08-31, real-
# hardware finding: light curtain trip while sitting on the pre-run screen,
# START never pressed, jumped straight to the run view -- confusing, since
# nothing had actually started) must stay on the pre-run screen too, now
# that it has its own SafetyBanner (see pre_run.py). Once a run IS active
# (_has_active_run), the run view's own banner still owns this correctly --
# unaffected, since that check is still gated on `not self._has_active_run`.
_PRE_RUN_ELIGIBLE_STATES = _IDLE_STATES | {State.E_STOP, State.PROTECTIVE_STOP}


class MainWindow(QMainWindow):
    """
    Receives the orchestrator instance and the UiBridge already wired into
    it (see main.py) so it can call orchestrator.operator_*() and react to
    bridge.state_changed/event_received.

    Screen routing (2026-08-30) needs BOTH the state and whether a run is
    currently open/resumable -- IDLE means two different things: "fresh or
    fully aborted" (show the pre-run setup screen) vs. "paused mid-run
    after a CYCLE STOP or a self-heal recovery" (stay on the run view with
    CONTINUE available, per FR-UI-04 -- re-showing the whole setup form
    there would force the operator to re-enter the field code and re-tap
    every station just to resume a run that never actually ended). Track
    RunStatusEvent.run_id (None = no open run) alongside State for this.
    """

    def __init__(self, orchestrator, bridge: UiBridge, config_dir: Path | None = None) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._bridge = bridge
        self._current_state = State.INITIALIZING
        self._has_active_run = False

        self.setWindowTitle("CIB Automated Sorting Cell")
        self.setMinimumSize(900, 640)
        self.setStyleSheet(theme.window_stylesheet())

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._pre_run_screen = PreRunScreen(orchestrator, config_dir=config_dir)
        self._pre_run_screen.start_requested.connect(self._show_run_view)
        self._pre_run_screen.connect_bridge(bridge)
        self._stack.addWidget(self._pre_run_screen)

        self._run_view_screen = RunViewScreen(orchestrator)
        self._run_view_screen.connect_bridge(bridge)
        self._stack.addWidget(self._run_view_screen)

        self._stack.setCurrentWidget(self._pre_run_screen)
        bridge.state_changed.connect(self._on_state_changed)
        bridge.event_received.connect(self._on_event)

    # ------------------------------------------------------------------

    def _on_state_changed(self, state: State) -> None:
        self._current_state = state
        self._update_screen()

    def _on_event(self, event: object) -> None:
        if isinstance(event, RunStatusEvent):
            self._has_active_run = event.run_id is not None
            self._update_screen()

    def _update_screen(self) -> None:
        if self._current_state in _PRE_RUN_ELIGIBLE_STATES and not self._has_active_run:
            self._stack.setCurrentWidget(self._pre_run_screen)
        else:
            self._stack.setCurrentWidget(self._run_view_screen)

    def _show_run_view(self) -> None:
        self._stack.setCurrentWidget(self._run_view_screen)

    # ------------------------------------------------------------------
    # Window close — ensure clean shutdown even if user clicks X
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Trigger hardware shutdown when the window is closed via X button.

        Without this, closing the window while a cycle is running skips
        camera disconnect — arena_api keeps the GVCP control channel open
        and the next startup fails with GC_ERR_TIMEOUT.
        """
        self._orchestrator.operator_shutdown()
        # Accept immediately — main.py's finally block will join the asyncio
        # thread and wait for shutdown to complete (up to 4+3+2 = 9s).
        event.accept()
