"""
cib/ui/app.py — Qt application entry point.

Called from main.py AFTER the asyncio event loop is already running
in a background thread.

Threading model:
    Main thread  → PyQt6 event loop (QApplication.exec())
    Daemon thread → asyncio event loop (orchestrator.run())

Communication:
    UI → orchestrator: orchestrator.operator_*() — thread-safe (asyncio Queue.put_nowait)
    orchestrator → UI: QTimer polls orchestrator.state every 500ms (read-only, no lock needed)
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from cib.ui.main_window import MainWindow


def run_qt(orchestrator) -> int:
    """
    Start PyQt6 in the calling (main) thread.
    Blocks until the window is closed.
    Returns the Qt exit code.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("CIB Automated Sorting Cell")

    window = MainWindow(orchestrator)
    window.show()

    return app.exec()
