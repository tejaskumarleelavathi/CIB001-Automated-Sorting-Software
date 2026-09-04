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
USE_REAL_ROBOT_REMOTE = True    # real Epson RC+ Remote Ethernet (controller lifecycle control)
USE_REAL_CAMERA_OVERHEAD = True    # real Lucid overhead camera (serial 253102168)
USE_REAL_CAMERA_EOA      = True    # real Lucid EoA camera (serial 253102169)
USE_REAL_LIGHTING = True    # real DCS-100E backlight
USE_REAL_SAFETY   = True    # real Sick Flexi safety controller
USE_REAL_PLC      = True    # real Logic PLC (status light / work light)

# Dev testing flag — inject a fake centroid if vision finds no bugs in the dish.
# Keeps the cycle running so you can test pick-place flow without a full dish.
# Set False when real bug detection is implemented and tested.
FORCE_SAMPLE_DETECTION = False
VIEW = False   # True = show image popups for 3s during testing | False = silent (production)
# Set False 2026-08-20 for the optimize-speed pass — each popup blocks the
# cycle for a hard 3s (cv2.waitKey(3000)), and there are 2 per cycle (~6s of
# 51s doing nothing but showing a debug window). See cycle.timing_summary's
# wall_total_s vs steps_total_s gap in cib/core/orchestrator.py.

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
    use_real_robot_remote: bool = False,
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
        # robot_remote (Remote Ethernet) — same real/mock split as robot
        # above. Actually connecting it still requires cfg.robot_remote.enabled
        # to be True (the one-time RC+ setup) -- the orchestrator gates every
        # call on that flag regardless of which driver object is here, so
        # building the real one before that setup exists is harmless (never used).
        robot_remote = (
            _make_real_robot_remote(cfg)
            if use_real_robot_remote
            else __import__("cib.drivers.robot_remote.mock", fromlist=["MockRemoteEthernetDriver"]).MockRemoteEthernetDriver()
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
            robot_remote    = robot_remote,
            safety          = safety,
            plc             = plc,
            pump            = MockPumpDriver(),
            camera_overhead = camera_overhead,
            camera_eoa      = camera_eoa,
            lighting        = lighting,
        )

    else:
        from cib.drivers.robot.driver    import EpsonT3BDriver
        # pump stays mocked even in full-real mode, same as the use_mocks
        # branch above (2026-09-04, first real HMI run -- USE_MOCKS=False
        # had never actually been exercised before this): cib/drivers/
        # pump/driver.py's MDriveDriver was never implemented (a stub) --
        # the real pipette motor is controlled entirely by sort_server.prg
        # talking directly to the Novanta actuator's own socket, never
        # through this PumpDriver Protocol at all. d.pump.connect() is
        # called at startup but aspirate()/dispense() are never called
        # anywhere in the orchestrator, so this is genuinely dead/inert
        # regardless of which pump driver is here -- confirmed, not a
        # gap that needs a real implementation.
        from cib.drivers.pump.mock       import MockPumpDriver
        return DriverSet(
            robot           = _make_real_robot(cfg),
            robot_remote    = _make_real_robot_remote(cfg),
            safety          = _make_real_safety(cfg),
            plc             = _make_real_plc(cfg),
            pump            = MockPumpDriver(),
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


def _make_real_robot_remote(cfg):
    from cib.drivers.robot_remote.driver import RemoteEthernetDriver
    rr = cfg.robot_remote
    return RemoteEthernetDriver(
        ip=rr.ip,
        port=rr.port,
        password=rr.password,
        connect_timeout_s=rr.connect_timeout_s,
        command_timeout_s=rr.command_timeout_s,
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
        use_real_robot_remote=USE_REAL_ROBOT_REMOTE,
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

    # Plate config folder for the pre-run screen's dropdown (FR-UI-02) --
    # same dev/prod split as the paths above.
    plate_configs_dir = (
        Path(__file__).parent / "config" / "plate_configs"
        if USE_MOCKS
        else Path(cfg.paths.plate_configs)
    )
    plate_configs_dir.mkdir(parents=True, exist_ok=True)

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

    # ── QApplication + UiBridge must exist before Orchestrator is
    # constructed -- on_state_change/on_event are constructor-only hooks,
    # and UiBridge is a QObject that must not be created before
    # QApplication exists. See cib/ui/app.py's module docstring.
    from PyQt6.QtWidgets import QApplication
    from cib.ui.bridge import UiBridge
    app = QApplication(sys.argv)
    app.setApplicationName("CIB Automated Sorting Cell")
    bridge = UiBridge()

    from cib.core.orchestrator import Orchestrator
    orchestrator = Orchestrator(
        drivers=drivers,
        on_state_change=bridge.on_state_change,
        on_event=bridge.on_event,
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
        plate_stations_cfg=cfg.plate_stations,
        db_path=db_path,
        runs_base_dir=runs_base_dir,
        field_code=DEV_FIELD_CODE,
        default_plate_id=DEV_PLATE_ID,
        robot_remote_enabled=cfg.robot_remote.enabled,
        safety_poll_interval_s=cfg.flexi.poll_interval_ms / 1000.0,
        plc_poll_interval_s=cfg.plc.poll_interval_ms / 1000.0,
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
            # Best-effort, last line of defense (2026-08-31, operator
            # request: "however the program closes... ensure the
            # backlight is turned off no matter what"). If the
            # orchestrator itself is what just crashed, its own
            # OperatorShutdownEvent handler (which normally turns this
            # off) never runs -- do it directly here instead, bypassing
            # the crashed orchestrator entirely. `loop` is still open at
            # this point (only the run() call inside it failed).
            try:
                loop.run_until_complete(
                    asyncio.wait_for(drivers.lighting.set_backlight(False), timeout=3.0)
                )
            except Exception as backlight_exc:
                log.warning("orchestrator.crash_backlight_off_failed", error=str(backlight_exc))

    asyncio_thread = threading.Thread(target=_run_asyncio, daemon=True, name="asyncio")
    asyncio_thread.start()

    # ── Run PyQt6 in main thread ──────────────────────────────────────────────
    from cib.ui.app import run_qt
    try:
        exit_code = run_qt(app, orchestrator, bridge, config_dir=plate_configs_dir)
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        # Qt window has closed.
        # If the operator used the SHUTDOWN button, operator_shutdown() was called and
        # the asyncio loop is already exiting cleanly (sends DISCONNECT to lighting).
        # 4.0 -> 12.0 / 3.0 -> 15.0 (2026-08-31, real-hardware finding): a
        # robot recovery can legitimately be in flight at shutdown time (the
        # keepalive loop's own background self-heal) and one attempt's
        # internal timeouts alone can total ~26s worst case
        # (wait_until_error_cleared 8s + wait_until_ready 10s +
        # wait_until_running 8s) -- the previous 4+3=7s budget force-stopped
        # the loop mid-recovery and crashed with "Event loop stopped before
        # Future completed." _recover_robot_connection() now also bails
        # between retries once shutdown is requested (see its
        # self._running check), so this margin only needs to cover one
        # attempt already in flight, not the full multi-retry sequence.
        asyncio_thread.join(timeout=12.0)
        if asyncio_thread.is_alive():
            # Window was closed without SHUTDOWN (e.g. X button or Ctrl+C).
            # Best-effort cleanup: request shutdown, wait a bit more, then force-stop.
            orchestrator.operator_shutdown()
            asyncio_thread.join(timeout=15.0)
            if asyncio_thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                asyncio_thread.join(timeout=2.0)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
