"""
cib/config/validator.py — Validation for machine config and plate config.

Raises ConfigValidationError with a clear message on any failure.
Called by loader.py — callers never call this directly.
"""

from __future__ import annotations

import ipaddress
import re


class ConfigValidationError(Exception):
    """Raised when a config file fails validation. Message is operator/engineer friendly."""
    pass


# ── Plate config validation ───────────────────────────────────────────────────

_WELL_RE = re.compile(r"^[A-Za-z]{1,2}\d{1,3}$")   # A1, H12, AA100 etc.


def validate_plate_rows(rows: list[dict], source: str = "plate config") -> None:
    """
    Validate a list of dicts loaded from the plate config Excel.
    Each dict must have keys: well, x, y (case-insensitive, already normalised by loader).

    Raises ConfigValidationError on first problem found.
    """
    if not rows:
        raise ConfigValidationError(
            f"{source}: file has no data rows. "
            "Add at least one well row below the header."
        )

    seen_wells: set[str] = set()

    for i, row in enumerate(rows, start=2):   # row 2 = first data row (row 1 = header)
        well = row.get("well")
        x    = row.get("x")
        y    = row.get("y")

        # Required fields present
        if well is None or str(well).strip() == "":
            raise ConfigValidationError(
                f"{source} row {i}: 'well' column is blank."
            )
        if x is None or str(x).strip() == "":
            raise ConfigValidationError(
                f"{source} row {i} (well={well}): 'x' column is blank."
            )
        if y is None or str(y).strip() == "":
            raise ConfigValidationError(
                f"{source} row {i} (well={well}): 'y' column is blank."
            )

        # Well label format
        well_str = str(well).strip()
        if not _WELL_RE.match(well_str):
            raise ConfigValidationError(
                f"{source} row {i}: well label '{well_str}' is not valid. "
                "Expected format like A1, B12, H6."
            )

        # Duplicate check
        well_upper = well_str.upper()
        if well_upper in seen_wells:
            raise ConfigValidationError(
                f"{source} row {i}: duplicate well label '{well_str}'. "
                "Each well must appear only once."
            )
        seen_wells.add(well_upper)

        # Numeric X / Y
        try:
            float(x)
        except (TypeError, ValueError):
            raise ConfigValidationError(
                f"{source} row {i} (well={well_str}): "
                f"x value '{x}' is not a number."
            )
        try:
            float(y)
        except (TypeError, ValueError):
            raise ConfigValidationError(
                f"{source} row {i} (well={well_str}): "
                f"y value '{y}' is not a number."
            )

        # Optional numeric columns — validate if present
        for col in ("aspirate_volume_ul", "aspirate_speed_ul_per_s",
                    "dispense_volume_ul", "dispense_speed_ul_per_s"):
            val = row.get(col)
            if val is not None and str(val).strip() != "":
                try:
                    if float(val) <= 0:
                        raise ConfigValidationError(
                            f"{source} row {i} (well={well_str}): "
                            f"'{col}' must be greater than 0, got {val}."
                        )
                except (TypeError, ValueError):
                    raise ConfigValidationError(
                        f"{source} row {i} (well={well_str}): "
                        f"'{col}' value '{val}' is not a number."
                    )


# ── Machine config validation ─────────────────────────────────────────────────

def validate_machine_config(cfg: dict) -> None:
    """
    Validate the parsed machine config dict.
    Raises ConfigValidationError with a clear message on any problem.
    """
    errors: list[str] = []

    _check_required_sections(cfg, errors)
    if errors:
        raise ConfigValidationError(
            "machine_config.yaml is missing required sections:\n  " +
            "\n  ".join(errors)
        )

    _check_robot(cfg.get("robot", {}), errors)
    _check_network_device("plc", cfg.get("plc", {}), errors)
    _check_network_device("flexi", cfg.get("flexi", {}), errors)
    _check_camera("overhead_camera", cfg.get("overhead_camera", {}), errors)
    _check_camera("eoa_camera", cfg.get("eoa_camera", {}), errors)
    _check_backlight(cfg.get("backlight", {}), errors)
    _check_pipette(cfg.get("pipette", {}), errors)
    _check_paths(cfg.get("paths", {}), errors)

    if errors:
        raise ConfigValidationError(
            "machine_config.yaml has validation errors:\n  " +
            "\n  ".join(errors)
        )


def _check_required_sections(cfg: dict, errors: list) -> None:
    required = ["robot", "plc", "flexi", "overhead_camera", "eoa_camera",
                "backlight", "pipette", "paths", "vision", "logging"]
    for section in required:
        if section not in cfg:
            errors.append(f"Missing section: '{section}'")


def _check_robot(robot: dict, errors: list) -> None:
    _require_ip(robot, "robot.ip", errors)
    _require_port(robot, "robot.port", errors)
    _require_positive(robot, "robot.connect_timeout_s", errors)
    _require_positive(robot, "robot.command_timeout_s", errors)


def _check_network_device(name: str, cfg: dict, errors: list) -> None:
    _require_ip(cfg, f"{name}.ip", errors)
    _require_port(cfg, f"{name}.port", errors)
    poll = cfg.get("poll_interval_ms")
    if poll is not None:
        try:
            if int(poll) <= 0:
                errors.append(f"{name}.poll_interval_ms must be > 0")
        except (TypeError, ValueError):
            errors.append(f"{name}.poll_interval_ms must be an integer")


def _check_camera(name: str, cfg: dict, errors: list) -> None:
    _require_ip(cfg, f"{name}.ip", errors)
    exp = cfg.get("exposure_us")
    if exp is not None:
        try:
            if float(exp) <= 0:
                errors.append(f"{name}.exposure_us must be > 0")
        except (TypeError, ValueError):
            errors.append(f"{name}.exposure_us must be a number")


def _check_backlight(cfg: dict, errors: list) -> None:
    _require_ip(cfg, "backlight.ip", errors)
    _require_port(cfg, "backlight.port", errors)
    ma = cfg.get("current_ma")
    if ma is not None:
        try:
            if not (0 < float(ma) <= 1000):
                errors.append("backlight.current_ma must be between 1 and 1000")
        except (TypeError, ValueError):
            errors.append("backlight.current_ma must be a number")


def _check_pipette(cfg: dict, errors: list) -> None:
    iface = cfg.get("interface", "")
    if iface not in ("tcp", "serial"):
        errors.append(f"pipette.interface must be 'tcp' or 'serial', got '{iface}'")
    for key in ("aspirate_volume_ul", "aspirate_speed_ul_per_s",
                "dispense_volume_ul", "dispense_speed_ul_per_s"):
        _require_positive(cfg, f"pipette.{key}", errors)


def _check_paths(cfg: dict, errors: list) -> None:
    for key in ("db", "plate_configs", "runs_output", "logs"):
        if not cfg.get(key):
            errors.append(f"paths.{key} is missing or empty")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_ip(cfg: dict, key: str, errors: list) -> None:
    section_key = key.split(".")[-1]
    val = cfg.get(section_key, "")
    if not val:
        # Empty IP is allowed for not-yet-configured devices (e.g. pipette)
        return
    try:
        ipaddress.ip_address(str(val))
    except ValueError:
        errors.append(f"{key}: '{val}' is not a valid IP address")


def _require_port(cfg: dict, key: str, errors: list) -> None:
    section_key = key.split(".")[-1]
    val = cfg.get(section_key)
    if val is None:
        errors.append(f"{key} is required")
        return
    try:
        port = int(val)
        if not (1 <= port <= 65535):
            errors.append(f"{key} must be between 1 and 65535, got {port}")
    except (TypeError, ValueError):
        errors.append(f"{key} must be an integer, got '{val}'")


def _require_positive(cfg: dict, key: str, errors: list) -> None:
    section_key = key.split(".")[-1]
    val = cfg.get(section_key)
    if val is None:
        errors.append(f"{key} is required")
        return
    try:
        if float(val) <= 0:
            errors.append(f"{key} must be > 0, got {val}")
    except (TypeError, ValueError):
        errors.append(f"{key} must be a number, got '{val}'")
