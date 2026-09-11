"""
cib/ui/screens/run_view.py — run monitoring + control screen (FR-UI-04/07/08/09/10).

State badge, run controls (CYCLE STOP/CONTINUE/ABORT/SHUTDOWN), the safety
banner, a camera panel (see cib/ui/widgets/camera_feed.py for why it's
"last capture", not live video), a run-stats row (field code/pick counts/
elapsed time, FR-UI-09), a scrolling log panel (FR-UI-10, capped at the
last 50 lines), and an error line below the log (2026-08-30 — the terminal
shows errors in red; a UI-only operator needs the same visibility).

CONTINUE (2026-08-30, FR-UI-04): this screen is only ever shown while IDLE
when a run is still open/resumable (see main_window.py's routing) -- e.g.
after a plain CYCLE STOP finishes, or after the Remote Ethernet self-heal
recovery lands back in IDLE. Pressing CONTINUE calls operator_start(),
which the orchestrator already treats as "resume the same run" rather than
opening a new one (see Orchestrator._ensure_run_open()) -- no re-entry of
field code/Plate IDs/station taps needed. START itself still only lives on
the pre-run screen, for a genuinely new/aborted run.

Fed by cib/ui/bridge.py's UiBridge signals (state_changed/event_received)
instead of the original main_window.py's 500ms polling -- connect_bridge()
wires this up once, called by the shell (MainWindow) after construction.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QGridLayout, QHBoxLayout, QLabel, QListWidget, QPushButton,
    QVBoxLayout, QWidget,
)

from cib.core.events import (
    ConnectionLostEvent,
    EoaFrameCapturedEvent,
    PLCEventLogEntry,
    RecoveryStatusEvent,
    RunStatusEvent,
    SafetyStatusEvent,
    SystemErrorEvent,
)
from cib.core.state_machine import State
from cib.ui import theme
from cib.ui.bridge import UiBridge
from cib.ui.widgets.camera_feed import CameraFeed
from cib.ui.widgets.safety_banner import SafetyBanner

_HINTS = {
    State.FAULT: (
        "FAULT: check the error below. If lighting failed to connect, click SHUTDOWN, "
        "wait ~30 s for the DCS-100E to clear its session, then restart."
    ),
    State.CYCLE_STOPPING: "Finishing current cycle — will stop when done.",
    State.IDLE: "Stopped mid-run. Press CONTINUE to resume, or ABORT to end this run.",
}

_LOG_MAX_ENTRIES = 50


class RunViewScreen(QWidget):
    def __init__(self, orchestrator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._run_started_at: datetime | None = None
        self._build_ui()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._elapsed_timer.start()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Sizing compacted for the 1024x600 HMI touchscreen (2026-09-11) --
        # see pre_run.py's _build_ui note for why (same desktop-scale
        # numbers here didn't fit either).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(6)

        self._state_label = QLabel("INITIALIZING")
        theme.apply_font(self._state_label, size=13, bold=True)
        self._state_label.setFixedHeight(32)
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._state_label)

        self._safety_banner = SafetyBanner()
        self._safety_banner.continue_clicked.connect(self._orchestrator.operator_acknowledge)
        self._safety_banner.abort_clicked.connect(self._orchestrator.operator_abort)
        outer.addWidget(self._safety_banner)

        # ── Run stats row (FR-UI-09) ────────────────────────────────────
        stats_card = QWidget()
        stats_card.setStyleSheet(theme.card_stylesheet())
        stats_row = QHBoxLayout(stats_card)
        stats_row.setContentsMargins(10, 6, 10, 6)
        self._field_code_stat = self._make_stat(stats_row, "Field code", "—")
        self._picked_stat = self._make_stat(stats_row, "Picked", "0")
        self._pass_stat = self._make_stat(stats_row, "Pass", "0")
        self._fail_stat = self._make_stat(stats_row, "Fail", "0")
        self._elapsed_stat = self._make_stat(stats_row, "Elapsed", "00:00:00")
        outer.addWidget(stats_card)

        content_row = QHBoxLayout()
        content_row.setSpacing(10)

        camera_card = QWidget()
        camera_card.setStyleSheet(theme.card_stylesheet())
        camera_layout = QVBoxLayout(camera_card)
        camera_layout.setContentsMargins(8, 8, 8, 8)
        self._camera_feed = CameraFeed()
        camera_layout.addWidget(self._camera_feed)
        content_row.addWidget(camera_card, 2)

        log_card = QWidget()
        log_card.setStyleSheet(theme.card_stylesheet())
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(8, 8, 8, 8)
        log_title = QLabel("Log")
        theme.apply_font(log_title, size=9, bold=True)
        log_title.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        log_layout.addWidget(log_title)
        self._log_list = QListWidget()
        self._log_list.setStyleSheet("border: none;")
        theme.apply_font(self._log_list, size=8)
        log_layout.addWidget(self._log_list)

        # Error line (2026-08-30) — the one thing the terminal shows in red
        # that a UI-only operator would otherwise never see. Separate from
        # the scrolling log above: this is the LATEST error only, kept
        # visible until the next state change clears it, not buried in
        # scrollback.
        self._error_label = QLabel("")
        theme.apply_font(self._error_label, size=9, bold=True)
        self._error_label.setStyleSheet(f"color: {theme.STATUS_RED};")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        log_layout.addWidget(self._error_label)

        content_row.addWidget(log_card, 1)

        outer.addLayout(content_row, stretch=1)

        self._hint_label = QLabel("")
        theme.apply_font(self._hint_label, size=8)
        self._hint_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-style: italic;")
        self._hint_label.setWordWrap(True)
        outer.addWidget(self._hint_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._stop_btn = QPushButton("CYCLE STOP")
        self._stop_btn.setFixedHeight(40)
        theme.apply_font(self._stop_btn, size=11, bold=True)
        self._stop_btn.setStyleSheet(theme.status_button_stylesheet(theme.STATUS_AMBER, "#b45309"))
        self._stop_btn.clicked.connect(self._orchestrator.operator_stop)
        btn_row.addWidget(self._stop_btn)

        self._continue_btn = QPushButton("CONTINUE")
        self._continue_btn.setFixedHeight(40)
        theme.apply_font(self._continue_btn, size=11, bold=True)
        self._continue_btn.setStyleSheet(theme.primary_button_stylesheet())
        self._continue_btn.clicked.connect(self._orchestrator.operator_start)
        btn_row.addWidget(self._continue_btn)

        self._abort_btn = QPushButton("ABORT")
        self._abort_btn.setFixedHeight(40)
        theme.apply_font(self._abort_btn, size=11, bold=True)
        self._abort_btn.setStyleSheet(theme.status_button_stylesheet(theme.STATUS_RED, "#b91c1c"))
        self._abort_btn.clicked.connect(self._orchestrator.operator_abort)
        btn_row.addWidget(self._abort_btn)

        self._shutdown_btn = QPushButton("SHUTDOWN")
        self._shutdown_btn.setFixedHeight(40)
        theme.apply_font(self._shutdown_btn, size=11, bold=True)
        self._shutdown_btn.setStyleSheet(theme.secondary_button_stylesheet())
        self._shutdown_btn.clicked.connect(self._on_shutdown)
        btn_row.addWidget(self._shutdown_btn)

        outer.addLayout(btn_row)

    def _make_stat(self, row: QHBoxLayout, label: str, initial: str) -> QLabel:
        col = QVBoxLayout()
        col.setSpacing(0)
        title = QLabel(label)
        theme.apply_font(title, size=8)
        title.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        value = QLabel(initial)
        theme.apply_font(value, size=12, bold=True)
        col.addWidget(title)
        col.addWidget(value)
        wrapper = QWidget()
        wrapper.setLayout(col)
        row.addWidget(wrapper)
        return value

    def _on_shutdown(self) -> None:
        self._shutdown_btn.setEnabled(False)
        self._orchestrator.operator_shutdown()
        QTimer.singleShot(3000, QApplication.quit)

    # ------------------------------------------------------------------
    # Bridge wiring
    # ------------------------------------------------------------------

    def connect_bridge(self, bridge: UiBridge) -> None:
        bridge.state_changed.connect(self._on_state_changed)
        bridge.event_received.connect(self._on_event)

    def _on_state_changed(self, state: State) -> None:
        label, bg, fg = theme.STATE_COLOURS.get(state, (str(state), theme.STATUS_GREY, "white"))
        self._state_label.setText(label)
        self._state_label.setStyleSheet(theme.state_badge_stylesheet(bg, fg))
        self._safety_banner.update_state(state)
        self._append_log(f"State: {label}")

        self._stop_btn.setEnabled(state in {State.RUNNING, State.CYCLE_STOPPING})
        # CONTINUE — this screen only shows while IDLE when a run is still
        # open (see main_window.py's routing), so IDLE here always means
        # "paused, resumable," never "fresh/aborted."
        self._continue_btn.setEnabled(state == State.IDLE)
        # ABORT is available from RUNNING too (2026-08-30, operator
        # request) -- a one-click "stop everything," not just after a
        # separate CYCLE STOP. The orchestrator makes this graceful: the
        # in-progress cycle still finishes (any already-picked specimen
        # gets dropped normally) before the run actually closes -- see
        # Orchestrator._abort_after_cycle. E_STOP/PROTECTIVE_STOP are
        # gated by the safety banner's own ABORT button instead (only
        # enabled once the hardware reset is physically confirmed — motor
        # power isn't available otherwise), so they're deliberately not
        # included in this button's own enable set.
        self._abort_btn.setEnabled(
            state in {State.RUNNING, State.IDLE, State.CYCLE_STOPPING, State.FAULT}
        )
        self._shutdown_btn.setEnabled(state != State.INITIALIZING)
        self._hint_label.setText(_HINTS.get(state, ""))

        # Clear the last error once the operator has actually acted on it
        # (resumed or aborted back out) — don't let a stale error linger
        # once it's no longer relevant.
        if state in {State.RUNNING, State.INITIALIZING}:
            self._error_label.setVisible(False)

    def _on_event(self, event: object) -> None:
        if isinstance(event, EoaFrameCapturedEvent):
            self._camera_feed.update_frame(event.frame)
        elif isinstance(event, SafetyStatusEvent):
            self._safety_banner.update_safety_status(event)
        elif isinstance(event, RecoveryStatusEvent):
            self._safety_banner.update_recovery_status(event.message)
        elif isinstance(event, PLCEventLogEntry):
            for entry in event.entries:
                self._append_log(entry)
        elif isinstance(event, RunStatusEvent):
            self._update_run_stats(event)
        elif isinstance(event, SystemErrorEvent):
            self._show_error(event.message)
            self._append_log(f"ERROR: {event.message}")
        elif isinstance(event, ConnectionLostEvent):
            message = f"{event.device.value} connection lost: {event.detail}"
            self._show_error(message)
            self._append_log(f"ERROR: {message}")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)

    def _update_run_stats(self, evt: RunStatusEvent) -> None:
        if evt.run_id is None:
            self._run_started_at = None
            self._field_code_stat.setText("—")
            self._picked_stat.setText("0")
            self._pass_stat.setText("0")
            self._fail_stat.setText("0")
            self._elapsed_stat.setText("00:00:00")
            return
        self._run_started_at = evt.started_at
        self._field_code_stat.setText(evt.field_code)
        self._pass_stat.setText(str(evt.pass_count))
        self._fail_stat.setText(str(evt.fail_count))
        self._picked_stat.setText(str(evt.pass_count + evt.fail_count))
        self._tick_elapsed()

    def _tick_elapsed(self) -> None:
        if self._run_started_at is None:
            return
        elapsed = datetime.now() - self._run_started_at
        total_seconds = max(0, int(elapsed.total_seconds()))
        h, rem = divmod(total_seconds, 3600)
        m, s = divmod(rem, 60)
        self._elapsed_stat.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _append_log(self, entry: str) -> None:
        self._log_list.addItem(entry)
        while self._log_list.count() > _LOG_MAX_ENTRIES:
            self._log_list.takeItem(0)
        self._log_list.scrollToBottom()
