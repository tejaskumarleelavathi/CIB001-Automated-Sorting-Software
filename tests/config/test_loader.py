"""
Tests for cib/config/loader.py and cib/config/validator.py
"""

import pytest
from pathlib import Path
import openpyxl
import yaml

from cib.config.loader import (
    load_machine_config,
    load_plate_config,
    list_plate_configs,
    PlateConfig,
    WellConfig,
)
from cib.config.validator import ConfigValidationError


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_xlsx(tmp_path: Path, rows: list[list], filename="test_plate.xlsx") -> Path:
    """Write a minimal Excel file with given rows (first row = header)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = tmp_path / filename
    wb.save(str(path))
    return path


def make_yaml(tmp_path: Path, overrides: dict = None) -> Path:
    """Write a valid machine_config.yaml, with optional key overrides."""
    base = {
        "robot": {"ip": "172.16.9.15", "port": 2000,
                  "connect_timeout_s": 5.0, "command_timeout_s": 10.0},
        "plc":   {"ip": "172.16.9.11", "port": 502, "poll_interval_ms": 100,
                  "unit_id": 1, "registers": {}, "coils": {}},
        "flexi": {"ip": "172.16.9.12", "port": 502, "poll_interval_ms": 100,
                  "unit_id": 1, "registers": {"status_base": 2000}},
        "overhead_camera": {"ip": "172.16.9.21", "serial": "",
                            "exposure_us": 10000, "gain_db": 0.0},
        "eoa_camera":      {"ip": "172.16.9.22", "serial": "",
                            "exposure_us": 5000, "gain_db": 0.0, "ip_original": "172.16.9.41"},
        "backlight": {"ip": "172.16.9.30", "port": 6006,
                      "current_ma": 250, "connect_timeout_s": 2.0},
        "ring_light": {"robot_output_pin": 12},
        "pipette": {"interface": "tcp", "ip": "", "port": 503,
                    "aspirate_volume_ul": 5.0, "aspirate_speed_ul_per_s": 2.0,
                    "dispense_volume_ul": 5.0, "dispense_speed_ul_per_s": 2.0},
        "paths": {"db": r"C:\LabAutomation\data\picks.db",
                  "plate_configs": r"C:\LabAutomation\config\plate_configs",
                  "runs_output":   r"C:\LabAutomation\data\runs",
                  "logs":          r"C:\LabAutomation\data\logs"},
        "vision":  {"detection_min_area_px": 50, "detection_max_area_px": 5000,
                    "backlight_lead_time_ms": 50},
        "logging": {"level": "INFO", "retention_days": 30},
    }
    if overrides:
        base.update(overrides)
    path = tmp_path / "machine_config.yaml"
    path.write_text(yaml.dump(base), encoding="utf-8")
    return path


VALID_PLATE_ROWS = [
    ["well", "x", "y"],
    ["A1", 123.4, 56.7],
    ["A2", 132.4, 56.7],
    ["B1", 123.4, 65.7],
]


# ── Plate config — happy path ─────────────────────────────────────────────────

def test_load_plate_config_basic(tmp_path):
    path = make_xlsx(tmp_path, VALID_PLATE_ROWS)
    cfg = load_plate_config(path)
    assert isinstance(cfg, PlateConfig)
    assert len(cfg.wells) == 3
    assert cfg.wells[0].well == "A1"
    assert cfg.wells[0].x == pytest.approx(123.4)
    assert cfg.wells[0].y == pytest.approx(56.7)


def test_load_plate_config_fill_order_preserved(tmp_path):
    rows = [["well", "x", "y"], ["B2", 1.0, 2.0], ["A1", 3.0, 4.0], ["C3", 5.0, 6.0]]
    cfg = load_plate_config(make_xlsx(tmp_path, rows))
    assert cfg.well_names() == ["B2", "A1", "C3"]


def test_well_names_uppercased(tmp_path):
    rows = [["well", "x", "y"], ["a1", 1.0, 2.0], ["b2", 3.0, 4.0]]
    cfg = load_plate_config(make_xlsx(tmp_path, rows))
    assert cfg.well_names() == ["A1", "B2"]


def test_well_by_name(tmp_path):
    cfg = load_plate_config(make_xlsx(tmp_path, VALID_PLATE_ROWS))
    w = cfg.well_by_name("A2")
    assert w is not None
    assert w.x == pytest.approx(132.4)


def test_well_by_name_case_insensitive(tmp_path):
    cfg = load_plate_config(make_xlsx(tmp_path, VALID_PLATE_ROWS))
    assert cfg.well_by_name("a1") is not None
    assert cfg.well_by_name("A1") is not None


def test_index_of(tmp_path):
    cfg = load_plate_config(make_xlsx(tmp_path, VALID_PLATE_ROWS))
    assert cfg.index_of("A1") == 0
    assert cfg.index_of("A2") == 1
    assert cfg.index_of("B1") == 2
    assert cfg.index_of("Z9") is None


def test_wells_from(tmp_path):
    cfg = load_plate_config(make_xlsx(tmp_path, VALID_PLATE_ROWS))
    remaining = cfg.wells_from("A2")
    assert [w.well for w in remaining] == ["A2", "B1"]


def test_wells_from_invalid_start(tmp_path):
    cfg = load_plate_config(make_xlsx(tmp_path, VALID_PLATE_ROWS))
    with pytest.raises(ConfigValidationError, match="Z9"):
        cfg.wells_from("Z9")


def test_optional_columns_parsed(tmp_path):
    rows = [
        ["well", "x", "y", "aspirate_volume_ul", "dispense_volume_ul"],
        ["A1", 1.0, 2.0, 3.5, 4.0],
    ]
    cfg = load_plate_config(make_xlsx(tmp_path, rows))
    assert cfg.wells[0].aspirate_volume_ul == pytest.approx(3.5)
    assert cfg.wells[0].dispense_volume_ul == pytest.approx(4.0)
    assert cfg.wells[0].aspirate_speed_ul_per_s is None   # not in file


def test_blank_rows_skipped(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["well", "x", "y"])
    ws.append(["A1", 1.0, 2.0])
    ws.append([None, None, None])   # blank row
    ws.append(["B1", 3.0, 4.0])
    path = tmp_path / "blanks.xlsx"
    wb.save(str(path))
    cfg = load_plate_config(path)
    assert len(cfg.wells) == 2


def test_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_plate_config(tmp_path / "nonexistent.xlsx")


# ── Plate config — validation errors ─────────────────────────────────────────

def test_empty_file_rejected(tmp_path):
    path = make_xlsx(tmp_path, [["well", "x", "y"]])  # header only, no data
    with pytest.raises(ConfigValidationError, match="no data rows"):
        load_plate_config(path)


def test_duplicate_well_rejected(tmp_path):
    rows = [["well", "x", "y"], ["A1", 1.0, 2.0], ["A1", 3.0, 4.0]]
    with pytest.raises(ConfigValidationError, match="duplicate"):
        load_plate_config(make_xlsx(tmp_path, rows))


def test_invalid_well_label_rejected(tmp_path):
    rows = [["well", "x", "y"], ["123bad", 1.0, 2.0]]
    with pytest.raises(ConfigValidationError, match="not valid"):
        load_plate_config(make_xlsx(tmp_path, rows))


def test_non_numeric_x_rejected(tmp_path):
    rows = [["well", "x", "y"], ["A1", "notanumber", 2.0]]
    with pytest.raises(ConfigValidationError, match="not a number"):
        load_plate_config(make_xlsx(tmp_path, rows))


def test_blank_x_rejected(tmp_path):
    rows = [["well", "x", "y"], ["A1", None, 2.0]]
    with pytest.raises(ConfigValidationError, match="'x' column is blank"):
        load_plate_config(make_xlsx(tmp_path, rows))


def test_negative_optional_volume_rejected(tmp_path):
    rows = [["well", "x", "y", "aspirate_volume_ul"], ["A1", 1.0, 2.0, -1.0]]
    with pytest.raises(ConfigValidationError, match="greater than 0"):
        load_plate_config(make_xlsx(tmp_path, rows))


# ── list_plate_configs ────────────────────────────────────────────────────────

def test_list_plate_configs(tmp_path):
    (tmp_path / "96well.xlsx").touch()
    (tmp_path / "48well.xlsx").touch()
    (tmp_path / "readme.txt").touch()   # should be ignored
    result = list_plate_configs(tmp_path)
    names = [p.name for p in result]
    assert "96well.xlsx" in names
    assert "48well.xlsx" in names
    assert "readme.txt" not in names


def test_list_plate_configs_empty_dir(tmp_path):
    assert list_plate_configs(tmp_path) == []


def test_list_plate_configs_missing_dir(tmp_path):
    assert list_plate_configs(tmp_path / "nonexistent") == []


# ── Machine config — happy path ───────────────────────────────────────────────

def test_load_machine_config_valid(tmp_path):
    path = make_yaml(tmp_path)
    cfg = load_machine_config(path)
    assert cfg.robot.ip == "172.16.9.15"
    assert cfg.robot.port == 2000
    assert cfg.plc.ip == "172.16.9.11"
    assert cfg.plc.poll_interval_ms == 100
    assert cfg.flexi.registers["status_base"] == 2000
    assert cfg.overhead_camera.exposure_us == 10000.0
    assert cfg.eoa_camera.ip_original == "172.16.9.41"
    assert cfg.backlight.current_ma == 250
    assert cfg.ring_light.robot_output_pin == 12
    assert cfg.pipette.aspirate_volume_ul == 5.0
    assert cfg.paths.db == r"C:\LabAutomation\data\picks.db"
    assert cfg.vision.backlight_lead_time_ms == 50
    assert cfg.logging.level == "INFO"


def test_machine_config_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_machine_config(tmp_path / "missing.yaml")


# ── Machine config — validation errors ───────────────────────────────────────

def test_machine_config_missing_section(tmp_path):
    cfg = make_yaml(tmp_path)
    raw = yaml.safe_load(cfg.read_text())
    del raw["robot"]
    cfg.write_text(yaml.dump(raw))
    with pytest.raises(ConfigValidationError, match="robot"):
        load_machine_config(cfg)


def test_machine_config_invalid_ip(tmp_path):
    path = make_yaml(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["robot"]["ip"] = "not.an.ip"
    path.write_text(yaml.dump(raw))
    with pytest.raises(ConfigValidationError, match="not a valid IP"):
        load_machine_config(path)


def test_machine_config_invalid_port(tmp_path):
    path = make_yaml(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["robot"]["port"] = 99999
    path.write_text(yaml.dump(raw))
    with pytest.raises(ConfigValidationError, match="65535"):
        load_machine_config(path)


def test_machine_config_invalid_pipette_interface(tmp_path):
    path = make_yaml(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["pipette"]["interface"] = "usb"
    path.write_text(yaml.dump(raw))
    with pytest.raises(ConfigValidationError, match="tcp.*serial"):
        load_machine_config(path)


def test_machine_config_zero_timeout_rejected(tmp_path):
    path = make_yaml(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["robot"]["connect_timeout_s"] = 0
    path.write_text(yaml.dump(raw))
    with pytest.raises(ConfigValidationError, match="> 0"):
        load_machine_config(path)
