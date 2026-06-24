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
USE_REAL_CAMERA   = True    # real Lucid GigE overhead camera
USE_REAL_LIGHTING = True    # real DCS-100E backlight

# Dev testing flag — inject a fake centroid if vision finds no bugs in the dish.
# Keeps the cycle running so you can test pick-place flow without a full dish.
# Set False when real bug detection is implemented and tested.
FORCE_SAMPLE_DETECTION = True
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


def build_drivers(use_mocks: bool, cfg, use_real_camera: bool, use_real_lighting: bool):
    from cib.core.orchestrator import DriverSet

    if use_mocks:
        from cib.drivers.robot.mock    import MockRobotDriver
        from cib.drivers.safety.mock   import MockSafetyDriver
        from cib.drivers.plc.mock      import MockPLCDriver
        from cib.drivers.pump.mock     import MockPumpDriver
        from cib.drivers.camera.mock   import MockCameraDriver
        from cib.drivers.lighting.mock import MockLightingDriver

        camera_overhead = (
            _make_real_camera(cfg, "overhead")
            if use_real_camera
            else MockCameraDriver(name="overhead", num_fake_samples=5)
        )
        lighting = (
            _make_real_lighting(cfg)
            if use_real_lighting
            else MockLightingDriver()
        )
        return DriverSet(
            robot           = MockRobotDriver(),
            safety          = MockSafetyDriver(),
            plc             = MockPLCDriver(),
            pump            = MockPumpDriver(),
            camera_overhead = camera_overhead,
            camera_eoa      = MockCameraDriver(name="eoa", num_fake_samples=1),
            lighting        = lighting,
        )

    else:
        from cib.drivers.robot.driver    import EpsonT3BDriver
        from cib.drivers.safety.driver   import FlexiCompactDriver
        from cib.drivers.plc.driver      import LogicPLCDriver
        from cib.drivers.pump.driver     import MDriveDriver
        return DriverSet(
            robot           = EpsonT3BDriver(),
            safety          = FlexiCompactDriver(),
            plc             = LogicPLCDriver(),
            pump            = MDriveDriver(),
            camera_overhead = _make_real_camera(cfg, "overhead"),
            camera_eoa      = _make_real_camera(cfg, "eoa"),
            lighting        = _make_real_lighting(cfg),
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
        name=which,
    )


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
    drivers = build_drivers(USE_MOCKS, cfg, USE_REAL_CAMERA, USE_REAL_LIGHTING)

    # Image save directory
    overhead_save_dir = (
        Path(__file__).parent / "output" / "overhead_images"
        if USE_MOCKS
        else Path(cfg.paths.overhead_images)
    )
    overhead_save_dir.mkdir(parents=True, exist_ok=True)

    from cib.core.orchestrator import Orchestrator
    orchestrator = Orchestrator(
        drivers=drivers,
        overhead_save_dir=overhead_save_dir if cfg.vision.overhead_save_images else None,
        overhead_save_format=cfg.vision.overhead_save_format,
        overhead_save_images=cfg.vision.overhead_save_images,
        force_sample_detection=FORCE_SAMPLE_DETECTION,
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
            structlog.get_logger(__name__).error("orchestrator.crashed", error=str(exc))

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
