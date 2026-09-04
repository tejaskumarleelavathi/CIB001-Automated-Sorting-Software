"""
cib/ui/widgets/camera_feed.py — overhead camera panel (FR-UI-08).

IMPORTANT LIMITATION, by design not oversight: this is NOT continuous
video. The overhead/EoA cameras are opened, captured a few times, and
closed once per pick cycle (confirmed by tracing every start_streaming/
capture/stop_streaming call in cib/core/orchestrator.py's _run_cycle()) --
there is no continuous stream to poll. So this panel shows the most
recent annotated frame from VisionOverheadResultEvent, refreshed once per
cycle (~tens of seconds), labeled with a "last capture" timestamp so the
operator isn't misled into expecting live video. True continuous video
would require changing how the orchestrator manages the camera
connection (keeping the stream open across cycles) -- out of scope here.

annotated_frame is a numpy ndarray (grayscale HxW or color HxWx3 BGR),
confirmed directly convertible to QImage without a copy through the
raw buffer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from cib.ui import theme


class CameraFeed(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        title = QLabel("Overhead camera")
        theme.apply_font(title, size=11, bold=True)
        title.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        outer.addWidget(title)

        self._image_label = QLabel("No capture yet")
        self._image_label.setMinimumSize(320, 240)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet(
            f"background-color: #f1f2f4; border: 1px solid {theme.CARD_BORDER}; "
            f"border-radius: 8px; color: {theme.TEXT_SECONDARY};"
        )
        self._image_label.setScaledContents(False)
        outer.addWidget(self._image_label, stretch=1)

        self._timestamp_label = QLabel("")
        theme.apply_font(self._timestamp_label, size=9)
        self._timestamp_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        outer.addWidget(self._timestamp_label)

    def update_frame(self, frame: Any) -> None:
        """frame: numpy ndarray, grayscale (H,W) or color BGR (H,W,3)."""
        if frame is None:
            return
        image = _ndarray_to_qimage(frame)
        if image is None:
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self._image_label.width(), self._image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(pixmap)
        self._timestamp_label.setText(f"Last capture: {datetime.now():%H:%M:%S}")


def _ndarray_to_qimage(frame: Any) -> QImage | None:
    try:
        import numpy as np
    except ImportError:
        return None
    if not isinstance(frame, np.ndarray):
        return None
    frame = np.ascontiguousarray(frame)
    if frame.ndim == 2:
        h, w = frame.shape
        return QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
    if frame.ndim == 3 and frame.shape[2] == 3:
        h, w, _ = frame.shape
        return QImage(frame.data, w, h, 3 * w, QImage.Format.Format_BGR888).copy()
    return None
