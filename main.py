"""
Entry point — CIB Automated Sample Sorting Cell.

Threading model:
    Main thread  → PyQt6 event loop  (required by Qt)
    Daemon thread → asyncio event loop (orchestrator, drivers, vision)

Set USE_MOCKS = True  → runs on laptop; hardware drivers replaced by mocks.
Set USE_MOCKS = False → real hardware (deploy on HMI only).

When USE_MOCKS = True you can selectively enable real hardware for bring-up:
    USE_REAL_CAMERA   = True  → real Lucid GigE overhead camera (arena_api)
    USE_REAL_LIGHTING = True  → real DCS-100E backlight controller
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

from cib.logging_setup import configure as configure_logging

# ── Mode flags ────────────────────────────────────────────────────────────────
USE_MOCKS = True       # True = laptop dev | False = HMI prod

# Selective real-hardware flags (only apply when USE_MOCKS = True)
# Set the device you physically have connected to True; leave others False.
USE_REAL_ROBOT    = True    # real Epson T3-B (sort_server.prg must be running in RC+)
USE_REAL_CAMERA_OVERHEAD = True    # real Lucid overhead camera (serial 253102168)
USE_REAL_CAMERA_EOA      = True    # real Lucid EoA camera (serial 253102169)
USE_REAL_LIGHTING = True    # real DCS-100E backlight
USE_REAL_SAFETY   = True    # real Sick Flexi safety controller
USE_REAL_PLC      = True    # real Logic PLC (status light / work light)

# Dev testing flag — inject a fake centroid if vision finds no bugs in the dish.
# Keeps the cycle running so you can test pick-place flow without a full dish.
# Set False when real bug detection is implemented and tested.
FORCE_SAMPLE_DETECTION = False
VIEW = True    # True = show image popups for 3s during testing | False = silent (production)

# Well-drop plate config — TEST-SCOPED (2026-08-11), NOT the final production
# design. See prd_updated.md Appendix D: the real design (who teaches wells,
# how often, vision-assisted or not) is still pending discussion with
# Austin/David. This just loads one fixed plate config file so pick+drop
# into real wells can be tested with the plate currently installed. Set to
# None to disable (falls back to the old (100.0, 100.0) placeholder).
# Generate this file with hardware_tests/teach_plate_wells.py.
TEST_PLATE_CONFIG = Path(__file__).parent / "config" / "plate_configs" / "test_96well.xlsx"
PLATE_U = 170.011   # from hardware_tests/plate_from_coords.py, teach pendant 2026-08-14

# Data-layer placeholders (2026-08-18) — the real pre-run setup screen
# (PRD FR-SETUP-01) doesn't exist yet, so there's nowhere for an operator to
# type these in. Every pick logged right now uses these fixed values until
# that screen is built. See prd checklist FR-SETUP-01/FR-RUN-02.
DEV_FIELD_CODE = "DEV"
DEV_PLATE_ID   = "DEV-PLATE"
# ─────────────────────────────────────────────────────────────────────────────

_DEV_CONFIG  = Path(__file__).parent / "config" / "machine_config.yaml"
_PROD_CONFIG = Path(r"C:\LabAutomation\config\machine_config.yaml")


def _load_config():
    from cib.config.loader import load_machine_config
    if _PROD_CONFIG.exists():
        return load_machine_config(_PROD_CONFIG)
    if _DEV_CONFIG.exists():
        return load_machine_config(_DEV_CONFIG)
    raise FileNotFoundError(
        f"machine_config.yaml not found at:\n  {_PROD_CONFIG}\n  {_DEV_CONFIG}"
    )


def build_drivers(
    use_mocks: bool,
    cfg,
    use_real_robot: bool,
    use_real_camera_overhead: bool,
    use_real_camera_eoa: bool,
    use_real_lighting: bool,
    use_real_safety: bool = False,
    use_real_plc: bool = False,
):
    from cib.core.orchestrator import DriverSet

    if use_mocks:
        from cib.drivers.safety.mock   import MockSafetyDriver
        from cib.drivers.plc.mock      import MockPLCDriver
        from cib.drivers.pump.mock     import MockPumpDriver
        from cib.drivers.camera.mock   import MockCameraDriver
        from cib.drivers.lighting.mock import MockLightingDriver

        robot = (
            _make_real_robot(cfg)
            if use_real_robot
            else __import__("cib.drivers.robot.mock", fromlist=["MockRobotDriver"]).MockRobotDriver()
        )
        camera_overhead = (
            _make_real_camera(cfg, "overhead")
            if use_real_camera_overhead
            else MockCameraDriver(name="overhead", num_fake_samples=5)
        )
        camera_eoa = (
            _make_real_camera(cfg, "eoa")
            if use_real_camera_eoa
            else MockCameraDriver(name="eoa", num_fake_samples=1)
        )
        lighting = (
            _make_real_lighting(cfg)
            if use_real_lighting
            else MockLightingDriver()
        )
        safety = _make_real_safety(cfg) if use_real_safety else MockSafetyDriver()
        plc    = _make_real_plc(cfg)    if use_real_plc    else MockPLCDriver()
        return DriverSet(
            robot           = robot,
            safety          = safety,
            plc             = plc,
            pump            = MockPumpDriver(),
            camera_overhead = camera_overhead,
            camera_eoa      = camera_eoa,
            lighting        = lighting,
        )

    else:
        from cib.drivers.robot.driver    import EpsonT3BDriver
        from cib.drivers.pump.driver     import MDriveDriver
        return DriverSet(
            robot           = _make_real_robot(cfg),
            safety          = _make_real_safety(cfg),
            plc             = _make_real_plc(cfg),
            pump            = MDriveDriver(),
            camera_overhead = _make_real_camera(cfg, "overhead"),
            camera_eoa      = _make_real_camera(cfg, "eoa"),
            lighting        = _make_real_lighting(cfg),
        )


def _make_real_robot(cfg):
    from cib.drivers.robot.driver import EpsonT3BDriver
    r = cfg.robot
    return EpsonT3BDriver(
        ip=r.ip,
        port=r.port,
        connect_timeout_s=r.connect_timeout_s,
        motion_timeout_s=getattr(r, "motion_timeout_s", 120.0),
    )


def _make_real_camera(cfg, which: str):
    from cib.drivers.camera.driver import GigECameraDriver
    cam_cfg = cfg.overhead_camera if which == "overhead" else cfg.eoa_camera
    return GigECameraDriver(
        serial=cam_cfg.serial,
        target_ip=cam_cfg.ip,        # force_ip to this address if camera is on wrong subnet
        exposure_us=cam_cfg.exposure_us,
        gain_db=cam_cfg.gain_db,
        pixel_format=cam_cfg.pixel_format,
        max_fps=getattr(cam_cfg, "max_fps", None),
        name=which,
    )


def _make_real_safety(cfg):
    from cib.drivers.safety.driver import FlexiCompactDriver
    return FlexiCompactDriver(ip=cfg.flexi.ip, port=cfg.flexi.port)


def _make_real_plc(cfg):
    from cib.drivers.plc.driver import LogicPLCDriver
    return LogicPLCDriver(ip=cfg.plc.ip, port=cfg.plc.port)


def _make_real_lighting(cfg):
    from cib.drivers.lighting.driver import LightingControllerDriver
    return LightingControllerDriver(
        ip=cfg.backlight.ip,
        port=cfg.backlight.port,
        current_ma=cfg.backlight.current_ma,
        connect_timeout_s=cfg.backlight.connect_timeout_s,
    )


def main() -> None:
    configure_logging()

    cfg = _load_config()
    drivers = build_drivers(
        USE_MOCKS, cfg,
        use_real_robot=USE_REAL_ROBOT,
        use_real_camera_overhead=USE_REAL_CAMERA_OVERHEAD,
        use_real_camera_eoa=USE_REAL_CAMERA_EOA,
        use_real_lighting=USE_REAL_LIGHTING,
        use_real_safety=USE_REAL_SAFETY,
        use_real_plc=USE_REAL_PLC,
    )

    # Image save directories
    overhead_save_dir = (
        Path(__file__).parent / "cib" / "output" / "overhead_images"
        if USE_MOCKS
        else Path(cfg.paths.overhead_images)
    )
    overhead_save_dir.mkdir(parents=True, exist_ok=True)

    eoa_save_dir = (
        Path(__file__).parent / "cib" / "output" / "eoa_images"
        if USE_MOCKS
        else Path(cfg.paths.runs_output)
    )
    eoa_save_dir.mkdir(parents=True, exist_ok=True)

    # Data layer (SQLite + per-run CSV) — dev-local db on the laptop, real
    # path on the HMI. See cfg.paths.db / cib/data/database.py.
    db_path = (
        Path(__file__).parent / "output" / "picks.db"
        if USE_MOCKS
        else Path(cfg.paths.db)
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)

    runs_base_dir = (
        Path(__file__).parent / "output" / "runs"
        if USE_MOCKS
        else Path(cfg.paths.runs_output)
    )
    runs_base_dir.mkdir(parents=True, exist_ok=True)

    # Test-scoped plate config (see TEST_PLATE_CONFIG comment above)
    plate_config = None
    if TEST_PLATE_CONFIG is not None and TEST_PLATE_CONFIG.exists():
        from cib.config.loader import load_plate_config
        try:
            plate_config = load_plate_config(TEST_PLATE_CONFIG)
            import structlog
            structlog.get_logger(__name__).info(
                "main.plate_config_loaded", path=str(TEST_PLATE_CONFIG),
                well_count=len(plate_config.wells),
            )
        except Exception as exc:
            import structlog
            structlog.get_logger(__name__).warning(
                "main.plate_config_load_failed", path=str(TEST_PLATE_CONFIG), error=str(exc),
            )

    from cib.core.orchestrator import Orchestrator
    orchestrator = Orchestrator(
        drivers=drivers,
        overhead_save_dir=overhead_save_dir if cfg.vision.overhead_save_images else None,
        overhead_save_format=cfg.vision.overhead_save_format,
        overhead_save_images=cfg.vision.overhead_save_images,
        eoa_save_dir=eoa_save_dir,
        force_sample_detection=FORCE_SAMPLE_DETECTION,
        view=VIEW,
        centroid_snap_mm=cfg.vision.centroid_snap_mm,
        eoa_normal_exposure_us=cfg.eoa_camera.exposure_us,
        eoa_backlight_on_exposure_us=cfg.eoa_camera.backlight_on_exposure_us,
        quadrant_split_y_mm=cfg.robot.quadrant_split_y_mm,
        u_top_deg=cfg.robot.u_top_deg,
        u_bottom_deg=cfg.robot.u_bottom_deg,
        plate_config=plate_config,
        plate_u=PLATE_U,
        db_path=db_path,
        runs_base_dir=runs_base_dir,
        field_code=DEV_FIELD_CODE,
        default_plate_id=DEV_PLATE_ID,
    )

    # ── Start asyncio in a background daemon thread ───────────────────────────
    # asyncio must NOT run on the main thread — PyQt6 needs the main thread.
    loop = asyncio.new_event_loop()

    def _run_asyncio():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(orchestrator.run())
        except Exception as exc:
            import structlog
            import traceback
            log = structlog.get_logger(__name__)
            log.error("orchestrator.crashed", error=str(exc))
            # orchestrator.run() uses asyncio.TaskGroup — a sub-task's real
            # exception is wrapped in an ExceptionGroup and str(exc) alone
            # hides it. Unwrap and print every sub-exception's full traceback.
            if hasattr(exc, "exceptions"):   # ExceptionGroup (Python 3.11+)
                for i, sub in enumerate(exc.exceptions):
                    print(f"\n--- sub-exception {i + 1} ---")
                    traceback.print_exception(type(sub), sub, sub.__traceback__)
            else:
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    asyncio_thread = threading.Thread(target=_run_asyncio, daemon=True, name="asyncio")
    asyncio_thread.start()

    # ── Run PyQt6 in main thread ──────────────────────────────────────────────
    from cib.ui.app import run_qt
    try:
        exit_code = run_qt(orchestrator)
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        # Qt window has closed.
        # If the operator used the SHUTDOWN button, operator_shutdown() was called and
        # the asyncio loop is already exiting cleanly (sends DISCONNECT to lighting).
        # Give it up to 4 seconds to finish; only force-stop if it didn't exit in time.
        asyncio_thread.join(timeout=4.0)
        if asyncio_thread.is_alive():
            # Window was closed without SHUTDOWN (e.g. X button or Ctrl+C).
            # Best-effort cleanup: request shutdown, wait a bit more, then force-stop.
            orchestrator.operator_shutdown()
            asyncio_thread.join(timeout=3.0)
            if asyncio_thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                asyncio_thread.join(timeout=2.0)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
