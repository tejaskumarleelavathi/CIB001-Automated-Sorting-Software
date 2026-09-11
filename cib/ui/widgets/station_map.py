"""
cib/ui/widgets/station_map.py — plate station tiles (FR-UI-01).

Entering a Plate ID for a station auto-activates that station's tile
(2026-09-10, operator request -- no separate tap needed). Clearing the
Plate ID again deactivates it. A tile can still be tapped directly: to
deactivate a station whose Plate ID you want to keep on file, or to
re-activate one you tapped off. Tapping a station that has no Plate ID is
still rejected with an inline prompt.

This widget only knows WHICH stations have a known Plate ID
(set_plate_id_known(), called by the parent pre-run screen whenever its
Plate ID text fields change) -- it doesn't own the Plate ID text fields
itself, keeping this widget reusable/testable on its own.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from cib.ui import theme


class StationMap(QWidget):
    selection_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tiles: dict[int, QPushButton] = {}
        self._plate_id_known: dict[int, bool] = {}
        self._active: set[int] = set()
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        title = QLabel("Plate stations — active once a Plate ID is entered")
        theme.apply_font(title, size=11, bold=True)
        title.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        outer.addWidget(title)

        self._grid = QGridLayout()
        self._grid.setSpacing(8)
        outer.addLayout(self._grid)

        self._prompt_label = QLabel("")
        theme.apply_font(self._prompt_label, size=9)
        self._prompt_label.setStyleSheet(f"color: {theme.STATUS_AMBER};")
        outer.addWidget(self._prompt_label)

    # ------------------------------------------------------------------

    def set_available_stations(self, station_nums: list[int]) -> None:
        """Rebuild the tile grid for whatever stations were found in the
        uploaded workbook. Deactivates/clears anything not in the new set."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._tiles = {}
        # The parent rebuilds its Plate ID text fields empty every time it
        # calls this, so any prior known-flags / active selections no longer
        # match anything on screen -- clear both. Without this, the
        # rising-edge auto-activate in set_plate_id_known() would misfire on
        # a field that now reads empty but is still remembered as "known"
        # from a previous workbook.
        self._plate_id_known = {}
        self._active = set()

        for i, num in enumerate(sorted(station_nums)):
            tile = QPushButton(f"Station {num}")
            tile.setCheckable(True)
            tile.setChecked(num in self._active)
            tile.setFixedHeight(64)
            theme.apply_font(tile, size=12, bold=True)
            tile.clicked.connect(lambda _checked, n=num: self._on_tile_clicked(n))
            self._grid.addWidget(tile, i // 2, i % 2)
            self._tiles[num] = tile
            self._restyle_tile(num)

        self.selection_changed.emit()

    def set_plate_id_known(self, station_num: int, known: bool) -> None:
        was_known = self._plate_id_known.get(station_num, False)
        self._plate_id_known[station_num] = known

        if not known and station_num in self._active:
            # Plate ID cleared -> drop the station.
            self._active.discard(station_num)
            if station_num in self._tiles:
                self._tiles[station_num].setChecked(False)
                self._restyle_tile(station_num)
            self.selection_changed.emit()
        elif known and not was_known and station_num not in self._active:
            # Rising edge: a Plate ID appeared for a station that had none.
            # Auto-activate it (2026-09-10, operator request -- no separate
            # tap). Only on the false->true edge, so once the operator taps
            # a station back OFF while keeping its Plate ID, further typing
            # in that field doesn't keep re-activating it.
            self._active.add(station_num)
            if station_num in self._tiles:
                self._tiles[station_num].setChecked(True)
                self._restyle_tile(station_num)
            self._prompt_label.setText("")
            self.selection_changed.emit()

    def active_stations(self) -> set[int]:
        return set(self._active)

    # ------------------------------------------------------------------

    def _on_tile_clicked(self, station_num: int) -> None:
        if not self._plate_id_known.get(station_num, False):
            self._tiles[station_num].setChecked(False)   # reject the tap
            self._prompt_label.setText(
                f"Enter a Plate ID for Station {station_num} before activating it."
            )
            return
        self._prompt_label.setText("")
        if self._tiles[station_num].isChecked():
            self._active.add(station_num)
        else:
            self._active.discard(station_num)
        self._restyle_tile(station_num)
        self.selection_changed.emit()

    def _restyle_tile(self, station_num: int) -> None:
        tile = self._tiles[station_num]
        if tile.isChecked():
            tile.setStyleSheet(theme.primary_button_stylesheet())
        else:
            tile.setStyleSheet(theme.secondary_button_stylesheet())
