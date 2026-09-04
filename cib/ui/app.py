"""
cib/ui/app.py — Qt application entry point.

Threading model:
    Main thread  → PyQt6 event loop (QApplication.exec())
    Daemon thread → asyncio event loop (orchestrator.run())

Communication:
    UI → orchestrator: orchestrator.operator_*() — thread-safe (asyncio Queue.put_nowait)
    orchestrator → UI: UiBridge (cib/ui/bridge.py) re-emits Orchestrator's
        on_state_change/on_event callbacks as Qt signals.

IMPORTANT ordering constraint (2026-08-27): QApplication() must exist
before ANY QObject is constructed, including UiBridge -- and UiBridge
must be constructed and passed into Orchestrator(...)'s on_state_change=/
on_event= kwargs BEFORE the asyncio thread starts (those are
constructor-only hooks). So main.py now creates QApplication and
UiBridge FIRST, then Orchestrator, then starts the asyncio thread, then
calls run_qt() (this file) just to build the window and start the Qt
event loop -- run_qt() no longer creates QApplication itself.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer

from cib.ui.main_window import MainWindow


def run_qt(app, orchestrator, bridge, config_dir: Path | None = None) -> int:
    """
    Build the window and start the Qt event loop in the calling (main)
    thread. QApplication (app) must already exist -- see main.py.
    Blocks until the window is closed. Returns the Qt exit code.
    """
    window = MainWindow(orchestrator, bridge, config_dir=config_dir)
    window.show()

    # Without this, Ctrl+C at the console does nothing while app.exec()
    # blocks (2026-08-31, operator request: Ctrl+C must trigger the same
    # clean shutdown as every other exit path) -- Qt's C++ event loop
    # doesn't hand control back to the Python interpreter often enough for
    # its SIGINT handler to actually fire. A harmless periodic timer tick
    # is the standard workaround: it wakes the interpreter regularly, so a
    # pending KeyboardInterrupt gets raised promptly instead of never.
    _sigint_pump = QTimer()
    _sigint_pump.timeout.connect(lambda: None)
    _sigint_pump.start(200)

    return app.exec()
