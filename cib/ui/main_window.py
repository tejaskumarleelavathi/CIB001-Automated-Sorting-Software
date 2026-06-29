"""
cib/ui/main_window.py — Minimal operator window (Phase 1 placeholder).

PURPOSE OF THIS FILE:
    This is a minimal working UI built so we can test the overhead camera
    and backlight pipeline while the full HMI UI is designed.

    What this window does:
      - START button → triggers one run (overhead captures loop continuously)
      - STOP  button → graceful cycle stop (finishes current cycle then parks)
      - State label  → shows current state from the state machine
      - Capture counter → counts images saved to output/overhead_images/

WHAT CHANGES WHEN FULL UI IS BUILT:
    This file will be replaced entirely. The real UI will have:
      - Pre-run setup screen (field code entry, plate ID, starting well selection)
      - Run view (live overhead camera feed, plate station map, per-well status)
      - Safety banner (e-stop indicator, light curtain status, reset button)
      - Camera feed widget (live image with annotated centroids)
      - Config manager screen (select plate config file)
    See architecture/overhead_camera.md and prd_updated.md for full spec.

THREADING NOTE:
    PyQt6 event loop must run in the main thread.
    asyncio event loop (orchestrator) runs in a background daemon thread.
    Communication: orchestrator.operator_*() methods use asyncio.Queue.put_nowait()
    which is thread-safe. UI polls orchestrator.state via QTimer every 500ms.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QApplication,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QCloseEvent, QFont

from cib.core.state_machine import State


# Colours for state badge
_STATE_COLOURS = {
    State.INITIALIZING:    ("INITIALIZING",    "#7f8c8d", "white"),
    State.IDLE:            ("IDLE — READY",    "#27ae60", "white"),
    State.RUNNING:         ("RUNNING",          "#2980b9", "white"),
    State.CYCLE_STOPPING:  ("STOPPING…",        "#e67e22", "white"),
    State.E_STOP:          ("E-STOP",           "#c0392b", "white"),
    State.PROTECTIVE_STOP: ("PROTECTIVE STOP",  "#e67e22", "white"),
    State.FAULT:           ("FAULT",            "#8e44ad", "white"),
}


class MainWindow(QMainWindow):
    """
    Minimal operator window.

    Receives the orchestrator instance so it can call operator_start/stop
    and read orchestrator.state. Polls state every 500ms via QTimer.
    """

    def __init__(self, orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._capture_count = 0

        self.setWindowTitle("CIB Automated Sorting Cell")
        self.setMinimumSize(480, 360)

        self._build_ui()

        # Poll orchestrator state every 500ms and refresh UI
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh)
        self._poll_timer.start(500)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(16)

        # ── Title ─────────────────────────────────────────────────────
        title = QLabel("CIB Automated Sorting Cell")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)

        outer.addWidget(_hline())

        # ── State badge ───────────────────────────────────────────────
        self._state_label = QLabel("INITIALIZING")
        state_font = QFont()
        state_font.setPointSize(20)
        state_font.setBold(True)
        self._state_label.setFont(state_font)
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setFixedHeight(56)
        self._state_label.setStyleSheet(
            "border-radius: 6px; background-color: #7f8c8d; color: white; padding: 4px;"
        )
        outer.addWidget(self._state_label)

        outer.addSpacing(8)

        # ── START / STOP buttons ──────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)

        self._start_btn = QPushButton("START")
        self._start_btn.setFixedHeight(72)
        _apply_font(self._start_btn, size=18, bold=True)
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; color: white; border-radius: 8px; }"
            "QPushButton:hover { background-color: #2ecc71; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self._start_btn.clicked.connect(self._on_start)
        self._start_btn.setEnabled(False)   # enabled once IDLE
        btn_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("STOP")
        self._stop_btn.setFixedHeight(72)
        _apply_font(self._stop_btn, size=18, bold=True)
        self._stop_btn.setStyleSheet(
            "QPushButton { background-color: #e67e22; color: white; border-radius: 8px; }"
            "QPushButton:hover { background-color: #f39c12; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        btn_row.addWidget(self._stop_btn)

        outer.addLayout(btn_row)

        # ── SHUTDOWN button ───────────────────────────────────────────
        self._shutdown_btn = QPushButton("SHUTDOWN")
        self._shutdown_btn.setFixedHeight(48)
        _apply_font(self._shutdown_btn, size=13, bold=True)
        self._shutdown_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; border-radius: 6px; }"
            "QPushButton:hover { background-color: #e74c3c; }"
            "QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }"
        )
        self._shutdown_btn.clicked.connect(self._on_shutdown)
        self._shutdown_btn.setEnabled(False)   # enabled once system is IDLE or FAULT
        outer.addWidget(self._shutdown_btn)

        outer.addWidget(_hline())

        # ── Info labels ───────────────────────────────────────────────
        self._capture_label = QLabel("Images captured this session: 0")
        self._capture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _apply_font(self._capture_label, size=11)
        outer.addWidget(self._capture_label)

        self._save_label = QLabel("Save folder:  output/overhead_images/")
        self._save_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _apply_font(self._save_label, size=10)
        self._save_label.setStyleSheet("color: #7f8c8d;")
        outer.addWidget(self._save_label)

        outer.addStretch()

        # Status / hint label — updated by _refresh based on state
        self._hint_label = QLabel("")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)
        _apply_font(self._hint_label, size=9)
        self._hint_label.setStyleSheet("color: #7f8c8d; font-style: italic;")
        outer.addWidget(self._hint_label)

        # Note about this being a placeholder
        note = QLabel(
            "Phase 1 placeholder — full HMI UI with camera feed and plate map coming next."
        )
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _apply_font(note, size=9)
        note.setStyleSheet("color: #95a5a6; font-style: italic;")
        outer.addWidget(note)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        self._orchestrator.operator_start()

    def _on_stop(self) -> None:
        self._orchestrator.operator_stop()

    def _on_shutdown(self) -> None:
        """
        Clean shutdown: tell orchestrator to disconnect all hardware (sends
        DISCONNECT to lighting controller), then close the application.
        The asyncio loop exits naturally — no force-stop, no WinError 10054 next time.
        """
        self._shutdown_btn.setEnabled(False)
        self._orchestrator.operator_shutdown()
        # Give asyncio ~3s to process the shutdown event, then close Qt.
        # main.py will join the asyncio thread; it exits cleanly without force-stop.
        QTimer.singleShot(3000, QApplication.quit)

    # ------------------------------------------------------------------
    # Window close — ensure clean shutdown even if user clicks X
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Trigger hardware shutdown when the window is closed via X button.

        Without this, closing the window while a cycle is running skips
        camera disconnect — arena_api keeps the GVCP control channel open
        and the next startup fails with GC_ERR_TIMEOUT.
        """
        self._poll_timer.stop()
        self._orchestrator.operator_shutdown()
        # Accept immediately — main.py's finally block will join the asyncio
        # thread and wait for shutdown to complete (up to 4+3+2 = 9s).
        event.accept()

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        state = self._orchestrator.state
        label, bg, fg = _STATE_COLOURS.get(state, (str(state), "#7f8c8d", "white"))

        self._state_label.setText(label)
        self._state_label.setStyleSheet(
            f"border-radius: 6px; background-color: {bg}; color: {fg}; padding: 4px;"
        )

        # Button enable logic
        self._start_btn.setEnabled(state == State.IDLE)
        self._stop_btn.setEnabled(state in {State.RUNNING, State.CYCLE_STOPPING})
        # SHUTDOWN: available in all states except INITIALIZING — operator must
        # always be able to shut down cleanly (e.g. when stuck in CYCLE_STOPPING).
        self._shutdown_btn.setEnabled(state != State.INITIALIZING)

        # Hint text
        _HINTS = {
            State.FAULT: (
                "FAULT: check logs.  If lighting failed to connect, click SHUTDOWN, "
                "wait ~30 s for the DCS-100E to clear its session, then restart."
            ),
            State.INITIALIZING: "Connecting to hardware — please wait…",
            State.IDLE: "Ready. Click START to begin sorting.",
            State.CYCLE_STOPPING: "Finishing current cycle — will stop when done.",
        }
        self._hint_label.setText(_HINTS.get(state, ""))

        # Count saved images
        self._update_capture_count()

    def _update_capture_count(self) -> None:
        """Count PNG/TIFF files in the overhead images folder."""
        try:
            from pathlib import Path
            folder = Path(__file__).parent.parent.parent / "output" / "overhead_images"
            if folder.exists():
                count = len(list(folder.glob("overhead_*")))
                if count != self._capture_count:
                    self._capture_count = count
                    self._capture_label.setText(
                        f"Images captured this session: {count}"
                    )
        except Exception:
            pass


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _apply_font(widget, size: int = 12, bold: bool = False) -> None:
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    widget.setFont(f)
