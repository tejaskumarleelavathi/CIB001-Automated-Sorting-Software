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
        self._light_curtain_stably_clear = False   # unknown/assume not-yet-proven-stable
        self._hardware_fault_active = True    # unknown/assume faulted until first poll
        # E-stop-specific physical-precondition tracking (2026-09-11) --
        # see _refresh()'s E_STOP branch for why these, not
        # _hardware_fault_active, gate CONTINUE there now.
        self._estop_active = True       # unknown/assume button still pressed until first poll
        self._reset_available = True    # unknown/assume blue button not yet pressed until first poll
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

        # Sizing compacted for the 1024x600 HMI touchscreen (2026-09-11) --
        # see pre_run.py's _build_ui note. This banner is only visible
        # during E_STOP/PROTECTIVE_STOP, but it still has to fit alongside
        # everything else on screen at that moment.
        self._card = QWidget()
        self._card.setStyleSheet(
            f"background-color: #fef2f2; border: 1px solid {theme.STATUS_RED}; border-radius: 8px;"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(2)

        self._title_label = QLabel("")
        theme.apply_font(self._title_label, size=11, bold=True)
        self._title_label.setStyleSheet(f"color: {theme.STATUS_RED};")
        card_layout.addWidget(self._title_label)

        self._detail_label = QLabel("")
        theme.apply_font(self._detail_label, size=9)
        self._detail_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._detail_label.setWordWrap(True)
        card_layout.addWidget(self._detail_label)

        # Recovery progress -- distinct styling from the alarm text above
        # (informational blue, not red) since this reports routine
        # progress ("Reconnecting to robot..."), not a new problem.
        self._status_label = QLabel("")
        theme.apply_font(self._status_label, size=9, bold=True)
        self._status_label.setStyleSheet(f"color: {theme.STATUS_BLUE};")
        self._status_label.setWordWrap(True)
        self._status_label.setVisible(False)
        card_layout.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        self._continue_btn = QPushButton("CONTINUE")
        self._continue_btn.setFixedHeight(32)
        theme.apply_font(self._continue_btn, size=10, bold=True)
        self._continue_btn.setStyleSheet(theme.primary_button_stylesheet())
        self._continue_btn.clicked.connect(self._on_continue_clicked)
        self._continue_btn.setEnabled(False)
        btn_row.addWidget(self._continue_btn)

        self._abort_btn = QPushButton("ABORT")
        self._abort_btn.setFixedHeight(32)
        theme.apply_font(self._abort_btn, size=10, bold=True)
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
        self._light_curtain_stably_clear = evt.light_curtain_stably_clear
        self._hardware_fault_active = evt.any_fault_active
        self._estop_active = evt.estop_active
        self._reset_available = evt.reset_available
        if evt.estop_active and evt.estop_button_index is not None:
            self._detail_label.setText(
                f"E-stop button #{evt.estop_button_index} pressed. "
                "Release the button, then press the blue reset button before CONTINUE."
            )
        elif evt.reset_available:
            # Button released but the blue reset button itself hasn't been
            # pressed yet (2026-09-11) -- distinct from the estop_active
            # case above: the button is no longer held, but the contactor
            # hasn't re-closed. CONTINUE still can't do anything about this
            # specific step -- it's the one physical action nothing in
            # software can substitute for.
            self._detail_label.setText(
                "E-stop button released. Press the physical blue reset button, "
                "then CONTINUE."
            )
        elif evt.light_curtain_tripped:
            self._detail_label.setText(
                "Light curtain obstructed. Clear the cell before CONTINUE."
            )
        elif not evt.light_curtain_stably_clear:
            # Reads clear right now but hasn't held clear long enough yet
            # to trust (2026-09-11 -- see LIGHT_CURTAIN_STABLE_CLEAR_S in
            # orchestrator.py: the raw reading flickers on this
            # installation, so a single "clear now" instant isn't enough).
            # Expected to only last ~2s after a genuine clear; if it lingers
            # much longer, the curtain itself likely has a real physical
            # problem (alignment, dust, a reflective surface) worth checking.
            self._detail_label.setText(
                "Light curtain just cleared — confirming it's stable before CONTINUE."
            )
        else:
            # 2026-09-11 fix: every branch above only ever SET this text --
            # none of them ever cleared it once the condition it described
            # stopped being true, so a stale message (e.g. "curtain
            # obstructed" from a hand that's since been pulled back out)
            # just sat there unchanged, no longer describing reality. Clear
            # it once nothing is actually blocking CONTINUE.
            self._detail_label.setText("")
        self._refresh()

    def _refresh(self) -> None:
        # CONTINUE and ABORT are gated DIFFERENTLY as of 2026-09-11 (OQ-23
        # resolved) -- they used to share one "hardware_ready" condition
        # (2026-08-30), back when neither button's own orchestrator handler
        # could do anything about a still-latched hardware fault except
        # wait for the physical blue button. Now CONTINUE's handler
        # actively sends a software reset pulse and VERIFIES it worked
        # before doing anything physical -- so CONTINUE only needs to gate
        # on the one physical precondition no software reset can work
        # around (curtain clear / e-stop button released+reset). ABORT's
        # handler was NOT given that same pulse-and-verify treatment (still
        # delivers a pending specimen with a plain robot move), so it stays
        # on the stricter original rule: motor power must ALREADY be
        # restored (_hardware_fault_active already False) before it's safe
        # to enable at all.
        if self._state == State.E_STOP:
            self._title_label.setText("E-STOP ACTIVE")
            self.setVisible(True)
            # Not gated on _hardware_fault_active -- pressing CONTINUE is
            # now what can finish clearing that (orchestrator sends the
            # same "Reset Motor" pulse the light-curtain path uses). Gated
            # instead on the physical preconditions that pulse itself
            # depends on: button not still pressed, blue reset button
            # already pressed (_reset_available), AND the curtain clear --
            # 2026-09-11 fix, found live: the P-Stop side's own Reset V1
            # (which that pulse targets) requires the curtain clear to
            # release too (same AND gate the PROTECTIVE_STOP branch below
            # depends on), so this was missing here and let CONTINUE show
            # as enabled with a hand physically inside the curtain.
            continue_ready = (
                not self._estop_active
                and not self._reset_available
                and self._light_curtain_stably_clear
                and not self._recovering
            )
            abort_ready = not self._hardware_fault_active and not self._recovering
            self._continue_btn.setEnabled(continue_ready)
            self._abort_btn.setEnabled(abort_ready)
        elif self._state == State.PROTECTIVE_STOP:
            self._title_label.setText("PROTECTIVE STOP — LIGHT CURTAIN")
            self.setVisible(True)
            # Curtain-clear is the one thing no software reset can work
            # around; _hardware_fault_active itself is what CONTINUE's own
            # pulse is meant to clear, so it's not a precondition here.
            continue_ready = self._light_curtain_stably_clear and not self._recovering
            abort_ready = (
                not self._hardware_fault_active
                and not self._light_curtain_tripped
                and not self._recovering
            )
            self._continue_btn.setEnabled(continue_ready)
            self._abort_btn.setEnabled(abort_ready)
        else:
            self.setVisible(False)
        # CONTINUE stays disabled the instant it's clicked (recovery can
        # take up to ~30s if the Remote Ethernet fallback restart fires) --
        # otherwise a second click mid-recovery races the first attempt.
        self._status_label.setVisible(self._recovering and bool(self._status_label.text()))
