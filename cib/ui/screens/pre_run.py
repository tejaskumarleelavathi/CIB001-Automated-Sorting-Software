"""
cib/ui/screens/pre_run.py — pre-run setup screen (FR-UI-01, FR-UI-02, FR-RUN-02/06).

Fields: field code, a Plate ID + optional starting well per discovered
station (from the selected multi-sheet workbook), and the station map
(a station activates as soon as its Plate ID is entered -- see
station_map.py). START is gated per FR-UI-01: "active only once all
required fields are filled and valid (field code entered, at least one
station active with a Plate ID, config file selected)" -- AND (2026-09-10,
operator request) only once the backend has finished starting up and
reached IDLE, so pressing it never fires an illegal start into a
still-INITIALIZING backend (which bounced the UI to the run view and
straight back). _start_hint spells out whichever precondition is missing.

Plate config selection (FR-UI-02, 2026-08-30): a dropdown auto-populated
from the files present in the HMI config folder (cib.config.loader.
list_plate_configs), not a file-browse dialog -- re-scanned every time
this screen becomes visible (showEvent) so a config dropped into the
folder while the app is running shows up without a restart, matching
FR-SETUP-08's "no software restart required."

Emits start_requested when the operator presses START with everything
already sent to the orchestrator via operator_configure_pre_run() +
operator_start() -- the parent shell (MainWindow) just needs to switch
to the run view, it doesn't need to know any of this screen's field state.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QTimer
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from cib.config.loader import PlateConfig, list_plate_configs, load_all_stations
from cib.config.validator import ConfigValidationError
from cib.core.events import RecoveryStatusEvent, SafetyStatusEvent
from cib.core.state_machine import State
from cib.ui import theme
from cib.ui.bridge import UiBridge
from cib.ui.widgets.safety_banner import SafetyBanner
from cib.ui.widgets.station_map import StationMap


class PreRunScreen(QWidget):
    start_requested = pyqtSignal()

    def __init__(self, orchestrator, config_dir: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._config_dir = config_dir
        self._loaded_stations: dict[int, PlateConfig] = {}
        self._plate_id_fields: dict[int, QLineEdit] = {}
        self._starting_well_fields: dict[int, QLineEdit] = {}
        # START stays disabled until the backend has finished connecting to
        # every device and reached IDLE (2026-09-10, operator request).
        # Assume not-ready until the first state_changed says otherwise --
        # same starting assumption main_window.py makes.
        self._backend_ready = False
        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Sizing throughout this screen (margins/spacing/fixed heights/font
        # sizes) targets the real HMI touchscreen -- 1024x600, see
        # main_window.py's setMinimumSize -- not a desktop monitor. The
        # original desktop-scale numbers (32/24 margins, 16 spacing, 56-64px
        # rows) needed ~850px of vertical space and visibly overlapped once
        # actually shown at 600px tall (2026-09-11, operator screenshot).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(6)

        title = QLabel("New Run — Setup")
        theme.apply_font(title, size=13, bold=True)
        outer.addWidget(title)

        # A safety fault (e-stop/light curtain) tripped before START was
        # ever pressed must be visible right here, not force a jump to the
        # run view (which implies a run is in progress when none has
        # started) -- see main_window.py's _PRE_RUN_ELIGIBLE_STATES.
        # show_abort=False (2026-08-31): no run is open here, so there's
        # nothing to abort — only CONTINUE (clear the fault, stay on this
        # setup screen) makes sense at this point.
        self._safety_banner = SafetyBanner(show_abort=False)
        self._safety_banner.continue_clicked.connect(self._orchestrator.operator_acknowledge)
        outer.addWidget(self._safety_banner)

        # ── Field code card ──────────────────────────────────────────
        field_card = QWidget()
        field_card.setStyleSheet(theme.card_stylesheet())
        field_layout = QVBoxLayout(field_card)
        field_layout.setContentsMargins(10, 6, 10, 6)
        field_layout.setSpacing(2)

        field_label = QLabel("Field code")
        theme.apply_font(field_label, size=9, bold=True)
        field_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        field_layout.addWidget(field_label)

        self._field_code_input = QLineEdit()
        self._field_code_input.setPlaceholderText("e.g. CIB-2026-001")
        self._field_code_input.setStyleSheet(theme.input_stylesheet())
        theme.apply_font(self._field_code_input, size=11)
        self._field_code_input.textChanged.connect(self._refresh_start_enabled)
        field_layout.addWidget(self._field_code_input)
        outer.addWidget(field_card)

        # ── Plate config selection card (FR-UI-02) ──────────────────────
        config_card = QWidget()
        config_card.setStyleSheet(theme.card_stylesheet())
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(10, 6, 10, 6)
        config_layout.setSpacing(4)

        config_label = QLabel("Plate config")
        theme.apply_font(config_label, size=9, bold=True)
        config_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        config_layout.addWidget(config_label)

        config_row = QHBoxLayout()
        self._config_combo = QComboBox()
        theme.apply_font(self._config_combo, size=10)
        self._config_combo.currentIndexChanged.connect(self._on_config_selected)
        config_row.addWidget(self._config_combo, 1)
        config_layout.addLayout(config_row)

        self._config_status_label = QLabel("")
        theme.apply_font(self._config_status_label, size=8)
        self._config_status_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        config_layout.addWidget(self._config_status_label)

        # Per-station Plate ID / starting-well fields, built once a
        # workbook is loaded (see _rebuild_station_fields).
        self._station_fields_grid = QGridLayout()
        self._station_fields_grid.setSpacing(3)
        config_layout.addLayout(self._station_fields_grid)

        self._station_map = StationMap()
        self._station_map.selection_changed.connect(self._refresh_start_enabled)
        config_layout.addWidget(self._station_map)

        outer.addWidget(config_card)
        outer.addStretch()

        # ── START / SHUTDOWN (FR-UI-03) ─────────────────────────────────
        # SHUTDOWN, not ABORT (2026-08-31, operator correction): this
        # screen only ever shows when no run is open (see
        # main_window.py's _PRE_RUN_ELIGIBLE_STATES) -- ABORT ends an
        # in-progress run, and there is never one to end here. The only
        # two real choices on this screen are "start a new cycle" or
        # "shut the whole system down."
        # Why START is unavailable right now -- so a disabled button isn't a
        # mystery (touchscreen: no tooltips). Empty once START is pressable.
        self._start_hint = QLabel("")
        theme.apply_font(self._start_hint, size=9)
        self._start_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        outer.addWidget(self._start_hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self._shutdown_btn = QPushButton("SHUTDOWN")
        self._shutdown_btn.setFixedHeight(40)
        theme.apply_font(self._shutdown_btn, size=11, bold=True)
        self._shutdown_btn.setStyleSheet(theme.secondary_button_stylesheet())
        self._shutdown_btn.clicked.connect(self._on_shutdown)
        btn_row.addWidget(self._shutdown_btn, 1)

        self._start_btn = QPushButton("START")
        self._start_btn.setFixedHeight(40)
        theme.apply_font(self._start_btn, size=12, bold=True)
        self._start_btn.setStyleSheet(theme.primary_button_stylesheet())
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn, 2)

        outer.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Bridge wiring: the safety banner, plus START's enable-gate which
    # now also tracks backend state (run stats/log still live on
    # RunViewScreen).
    # ------------------------------------------------------------------

    def connect_bridge(self, bridge: UiBridge) -> None:
        bridge.state_changed.connect(self._safety_banner.update_state)
        bridge.state_changed.connect(self._on_state_changed)
        bridge.event_received.connect(self._on_event)

    def _on_state_changed(self, state: State) -> None:
        # IDLE is the only state START is actually valid from on this screen:
        # INITIALIZING rejects it (illegal transition -- the UI would flip to
        # the run view then bounce straight back here once IDLE arrives),
        # E_STOP needs a hardware reset, PROTECTIVE_STOP goes through the
        # safety banner's CONTINUE. So gate purely on == IDLE.
        self._backend_ready = state == State.IDLE
        self._refresh_start_enabled()

    def _on_event(self, event: object) -> None:
        if isinstance(event, SafetyStatusEvent):
            self._safety_banner.update_safety_status(event)
        elif isinstance(event, RecoveryStatusEvent):
            self._safety_banner.update_recovery_status(event.message)

    # ------------------------------------------------------------------
    # Plate config dropdown (FR-UI-02)
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        """Re-scan the config folder every time this screen becomes
        visible — a config dropped in while the app is running shows up
        without a restart (FR-SETUP-08), no separate refresh button needed."""
        super().showEvent(event)
        self._refresh_config_list()

    def _refresh_config_list(self) -> None:
        if self._config_dir is None:
            return
        previous = self._config_combo.currentData()
        files = list_plate_configs(self._config_dir)

        self._config_combo.blockSignals(True)
        self._config_combo.clear()
        if not files:
            self._config_combo.addItem("No plate config files found", None)
            self._config_status_label.setText(f"Checked: {self._config_dir}")
        else:
            for path in files:
                self._config_combo.addItem(path.name, str(path))

        # Restore the previous selection if it still exists, else default
        # to the first real file, if any — a fresh screen shouldn't be
        # left on a blank selection the operator has to notice and fix
        # themselves.
        idx = self._config_combo.findData(previous) if previous is not None else -1
        if idx < 0 and files:
            idx = 0
        if idx >= 0:
            self._config_combo.setCurrentIndex(idx)
        self._config_combo.blockSignals(False)

        # Bug found 2026-09-01: this used to rely on setCurrentIndex()
        # above emitting currentIndexChanged to trigger _on_config_selected.
        # QComboBox auto-sets currentIndex to 0 as the FIRST item gets
        # added back in the clear()+repopulate loop above -- so whenever
        # the resolved idx here is ALSO 0 (the common case: same file as
        # last time, or just the first file in the folder), setCurrentIndex
        # is a same-value no-op and the signal never fires. The dropdown
        # then visibly showed the right file selected while the Plate ID /
        # starting-well fields below stayed stale/empty from whatever was
        # loaded (or not) before this refresh — confirmed by the operator:
        # picking a different file and picking this one back fixed it,
        # because THAT round-trip always produces a real index change.
        # Fix: call this explicitly and unconditionally instead of ever
        # depending on the signal.
        self._on_config_selected(self._config_combo.currentIndex())

    def _on_config_selected(self, _index: int) -> None:
        path_str = self._config_combo.currentData()
        if not path_str:
            self._loaded_stations = {}
            self._rebuild_station_fields()
            self._refresh_start_enabled()
            return
        path = Path(path_str)
        try:
            stations = load_all_stations(path)
        except (FileNotFoundError, ConfigValidationError) as exc:
            self._config_status_label.setText(f"Failed to load {path.name}: {exc}")
            self._config_status_label.setStyleSheet(f"color: {theme.STATUS_RED};")
            self._loaded_stations = {}
            self._rebuild_station_fields()
            self._refresh_start_enabled()
            return
        self._loaded_stations = stations
        self._config_status_label.setText(f"{len(stations)} station(s) found in {path.name}")
        self._config_status_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self._rebuild_station_fields()

    def _rebuild_station_fields(self) -> None:
        while self._station_fields_grid.count():
            item = self._station_fields_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._plate_id_fields = {}
        self._starting_well_fields = {}

        for row, num in enumerate(sorted(self._loaded_stations.keys())):
            label = QLabel(f"Station {num}")
            theme.apply_font(label, size=10, bold=True)
            self._station_fields_grid.addWidget(label, row, 0)

            plate_id_field = QLineEdit()
            plate_id_field.setPlaceholderText("Plate ID")
            plate_id_field.setStyleSheet(theme.input_stylesheet())
            theme.apply_font(plate_id_field, size=10)
            # Capped, not left to the grid's default even-stretch (2026-09-01
            # UI pass, operator feedback — "so expanded... characters look
            # small"): with nothing else constraining column width, a
            # QGridLayout splits leftover horizontal space evenly across
            # every column that has one, so on a wide HMI screen these
            # boxes stretched to hundreds of pixels for a 4-6 character
            # Plate ID. The unused column 3 stretch below (not any widget
            # here) now absorbs that leftover space instead.
            plate_id_field.setMaximumWidth(150)
            plate_id_field.textChanged.connect(
                lambda text, n=num: self._on_plate_id_changed(n, text)
            )
            self._station_fields_grid.addWidget(plate_id_field, row, 1)
            self._plate_id_fields[num] = plate_id_field

            starting_well_field = QLineEdit()
            starting_well_field.setPlaceholderText("Starting well (optional, e.g. C4)")
            starting_well_field.setStyleSheet(theme.input_stylesheet())
            theme.apply_font(starting_well_field, size=10)
            starting_well_field.setMaximumWidth(200)
            self._station_fields_grid.addWidget(starting_well_field, row, 2)
            self._starting_well_fields[num] = starting_well_field

        # Absorbs whatever horizontal space the two capped input columns
        # don't use, instead of the grid stretching those inputs to fill
        # the card (see the width-cap comment above).
        self._station_fields_grid.setColumnStretch(3, 1)

        self._station_map.set_available_stations(sorted(self._loaded_stations.keys()))

    def _on_plate_id_changed(self, station_num: int, text: str) -> None:
        self._station_map.set_plate_id_known(station_num, bool(text.strip()))
        self._refresh_start_enabled()

    # ------------------------------------------------------------------
    # START gating (FR-UI-01) + submit
    # ------------------------------------------------------------------

    def _refresh_start_enabled(self) -> None:
        has_field_code = bool(self._field_code_input.text().strip())
        has_active_station = len(self._station_map.active_stations()) > 0
        has_config = bool(self._loaded_stations)
        ready = (
            self._backend_ready and has_field_code and has_active_station and has_config
        )
        self._start_btn.setEnabled(ready)

        if not self._backend_ready:
            hint = "System still starting up... START will enable once it's ready."
        elif not has_config:
            hint = "Select a plate config."
        elif not has_field_code:
            hint = "Enter a field code."
        elif not has_active_station:
            hint = "Enter a Plate ID for at least one station."
        else:
            hint = ""
        self._start_hint.setText(hint)

    def _on_start(self) -> None:
        # Defensive: the button is disabled until _backend_ready, but never
        # fire operator_start() into a not-yet-IDLE backend even if somehow
        # reached -- that's the illegal transition that bounced the UI.
        if not self._backend_ready:
            return
        active = self._station_map.active_stations()
        field_code = self._field_code_input.text().strip()
        plate_ids = {
            num: self._plate_id_fields[num].text().strip()
            for num in active
        }
        starting_wells = {
            num: (self._starting_well_fields[num].text().strip() or None)
            for num in active
        }
        selected = {num: self._loaded_stations[num] for num in active}

        self._orchestrator.operator_configure_pre_run(
            field_code=field_code,
            plate_ids=plate_ids,
            starting_wells=starting_wells,
            selected=selected,
        )
        self._orchestrator.operator_start()
        self.start_requested.emit()

    def _on_shutdown(self) -> None:
        self._shutdown_btn.setEnabled(False)
        self._orchestrator.operator_shutdown()
        QTimer.singleShot(3000, QApplication.quit)
