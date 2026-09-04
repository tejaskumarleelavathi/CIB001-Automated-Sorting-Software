"""
cib/ui/widgets/safety_banner.py — E-stop / light-curtain alert banner + CONTINUE.

Implements FR-UI-05 (light curtain protective stop, CONTINUE disabled until
physically clear) and FR-UI-06 (e-stop alert with source detail, CONTINUE
disabled until hardware reset completes). The exact gating rule (light
curtain must show clear on the live 100ms safety poll, hardware fault must
show cleared) was already proven correct in the original
cib/ui/main_window.py's _refresh() -- reproduced here, driven by
SafetyStatusEvent pushed through cib/ui/bridge.py instead of polling
orchestrator.hardware_fault_active/light_curtain_tripped every 500ms.

Quiet (hidden) in every state except E_STOP/PROTECTIVE_STOP -- this is an
alert banner, not a permanent status strip (the state badge elsewhere on
the run view already shows the current state at all times).
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from cib.core.events import SafetyStatusEvent
from cib.core.state_machine import State
from cib.ui import theme


class SafetyBanner(QWidget):
    continue_clicked = pyqtSignal()
    abort_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, show_abort: bool = True) -> None:
        """
        show_abort: hide the ABORT button entirely (2026-08-31) — on the
        pre-run screen a safety fault can trip before START was ever
        pressed, with no run open and nothing in the pipette to abort;
        ABORT there would be functionally near-identical to CONTINUE
        (clear fault, home robot) but implies a run is being cancelled
        when none exists. Set False by PreRunScreen; RunViewScreen keeps
        the default True, where ABORT can genuinely deliver an in-transit
        specimen before ending the run.
        """
        super().__init__(parent)
        self._light_curtain_tripped = True   # unknown/assume tripped until first poll
        self._hardware_fault_active = True    # unknown/assume faulted until first poll
        self._state = State.INITIALIZING
        self._show_abort = show_abort
        # Set the instant CONTINUE is clicked, cleared on the next real
        # state change (2026-08-31) -- recovery can now take up to ~30s
        # (Remote Ethernet fallback restart), and without this the button
        # just visibly did nothing for that whole window.
        self._recovering = False
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QWidget()
        self._card.setStyleSheet(
            f"background-color: #fef2f2; border: 1px solid {theme.STATUS_RED}; border-radius: 10px;"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 12, 16, 12)

        self._title_label = QLabel("")
        theme.apply_font(self._title_label, size=13, bold=True)
        self._title_label.setStyleSheet(f"color: {theme.STATUS_RED};")
        card_layout.addWidget(self._title_label)

        self._detail_label = QLabel("")
        theme.apply_font(self._detail_label, size=10)
        self._detail_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._detail_label.setWordWrap(True)
        card_layout.addWidget(self._detail_label)

        # Recovery progress -- distinct styling from the alarm text above
        # (informational blue, not red) since this reports routine
        # progress ("Reconnecting to robot..."), not a new problem.
        self._status_label = QLabel("")
        theme.apply_font(self._status_label, size=10, bold=True)
        self._status_label.setStyleSheet(f"color: {theme.STATUS_BLUE};")
        self._status_label.setWordWrap(True)
        self._status_label.setVisible(False)
        card_layout.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        self._continue_btn = QPushButton("CONTINUE")
        self._continue_btn.setFixedHeight(40)
        theme.apply_font(self._continue_btn, size=12, bold=True)
        self._continue_btn.setStyleSheet(theme.primary_button_stylesheet())
        self._continue_btn.clicked.connect(self._on_continue_clicked)
        self._continue_btn.setEnabled(False)
        btn_row.addWidget(self._continue_btn)

        self._abort_btn = QPushButton("ABORT")
        self._abort_btn.setFixedHeight(40)
        theme.apply_font(self._abort_btn, size=12, bold=True)
        self._abort_btn.setStyleSheet(
            theme.status_button_stylesheet(theme.STATUS_RED, "#b91c1c")
        )
        self._abort_btn.clicked.connect(self.abort_clicked.emit)
        self._abort_btn.setEnabled(False)
        if self._show_abort:
            btn_row.addWidget(self._abort_btn)

        card_layout.addLayout(btn_row)
        outer.addWidget(self._card)

    # ------------------------------------------------------------------
    # External updates -- connect these to the UiBridge's signals
    # ------------------------------------------------------------------

    def update_state(self, state: State) -> None:
        if state != self._state:
            # A real transition -- whatever recovery was in flight either
            # finished (left E_STOP/PROTECTIVE_STOP, banner hides below)
            # or a fresh, distinct fault just started. Either way the old
            # "recovering" status no longer applies.
            self._recovering = False
            self._status_label.setVisible(False)
        self._state = state
        self._refresh()

    def update_recovery_status(self, message: str) -> None:
        """Progress update while CONTINUE's recovery is running — see
        RecoveryStatusEvent."""
        self._status_label.setText(message)
        self._status_label.setVisible(self._recovering)

    def _on_continue_clicked(self) -> None:
        self._recovering = True
        self._status_label.setText("Recovering...")
        self._refresh()
        self.continue_clicked.emit()

    def update_safety_status(self, evt: SafetyStatusEvent) -> None:
        self._light_curtain_tripped = evt.light_curtain_tripped
        self._hardware_fault_active = evt.any_fault_active
        if evt.estop_active and evt.estop_button_index is not None:
            self._detail_label.setText(
                f"E-stop button #{evt.estop_button_index} pressed. "
                "Release the button, then press the blue reset button before CONTINUE."
            )
        elif evt.light_curtain_tripped:
            self._detail_label.setText(
                "Light curtain obstructed. Clear the cell before CONTINUE."
            )
        self._refresh()

    def _refresh(self) -> None:
        # ABORT is gated the same as CONTINUE (2026-08-30, operator
        # request) -- both need motor power actually restored to do
        # anything physically meaningful: ABORT still delivers an
        # already-picked specimen before closing the run (same as
        # CONTINUE would), which is a real robot move that cannot
        # succeed while the hardware fault is still active. Disabled
        # (grayed out), not hidden, same visual treatment as CONTINUE.
        if self._state == State.E_STOP:
            self._title_label.setText("E-STOP ACTIVE")
            self.setVisible(True)
            hardware_ready = not self._hardware_fault_active and not self._recovering
            self._continue_btn.setEnabled(hardware_ready)
            self._abort_btn.setEnabled(hardware_ready)
        elif self._state == State.PROTECTIVE_STOP:
            self._title_label.setText("PROTECTIVE STOP — LIGHT CURTAIN")
            self.setVisible(True)
            hardware_ready = (
                not self._hardware_fault_active
                and not self._light_curtain_tripped
                and not self._recovering
            )
            self._continue_btn.setEnabled(hardware_ready)
            self._abort_btn.setEnabled(hardware_ready)
        else:
            self.setVisible(False)
        # CONTINUE stays disabled the instant it's clicked (recovery can
        # take up to ~30s if the Remote Ethernet fallback restart fires) --
        # otherwise a second click mid-recovery races the first attempt.
        self._status_label.setVisible(self._recovering and bool(self._status_label.text()))
