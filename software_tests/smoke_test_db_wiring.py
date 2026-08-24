"""
smoke_test_db_wiring.py — one-off verification that the data layer (SQLite
+ CSV) is actually wired into a real running orchestrator, not just proven
in isolated unit tests.

Runs a handful of cycles against full mock drivers (force_sample_detection
so it doesn't need a real camera frame), then reads back the actual DB rows
and actual CSV file from disk and prints them.

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" software_tests/smoke_test_db_wiring.py
"""

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.core.events import AllDevicesReadyEvent, OperatorAbortEvent, OperatorStartEvent, OperatorStopEvent
from cib.core.orchestrator import DriverSet, Orchestrator
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.robot_remote.mock import MockRemoteEthernetDriver
from cib.drivers.safety.mock import MockSafetyDriver

# Short path OUTSIDE the repo — this repo's own path is already ~140 chars
# deep (KTM Research\...\CIB001 Automated Sorting Prototype\...), and nesting
# run_id subfolders + long CSV filenames under it trips Windows' 260-char
# MAX_PATH limit. Real risk worth knowing, but not what this script tests.
OUT_DIR = Path(os.environ.get("TEMP", r"C:\Temp")) / f"cib_smoke_{int(time.time())}"


async def main() -> None:
    OUT_DIR.mkdir(parents=True)

    drivers = DriverSet(
        robot=MockRobotDriver(),
        robot_remote=MockRemoteEthernetDriver(),
        safety=MockSafetyDriver(),
        plc=MockPLCDriver(),
        pump=MockPumpDriver(),
        camera_overhead=MockCameraDriver(name="overhead", num_fake_samples=3),
        camera_eoa=MockCameraDriver(name="eoa", num_fake_samples=1),
        lighting=MockLightingDriver(),
    )
    orch = Orchestrator(
        drivers=drivers,
        db_path=OUT_DIR / "picks.db",
        runs_base_dir=OUT_DIR / "runs",
        force_sample_detection=True,   # inject a fake centroid so cycles actually run
        field_code="SMOKE-TEST",
        default_plate_id="SMOKE-PLATE",
    )

    from cib.core.state_machine import State

    run_task = asyncio.create_task(orch.run())
    for _ in range(50):   # up to 5s
        if orch.state == State.IDLE:
            break
        await asyncio.sleep(0.1)
    print(f"State after startup: {orch.state}")

    orch.operator_start()
    print("Started. Letting a few cycles run...")
    for i in range(16):
        await asyncio.sleep(0.5)
        active_tasks = [t for t in asyncio.all_tasks() if not t.done()]
        print(f"  t={i*0.5:.1f}s state={orch.state} cycle_active={orch._cycle_active} "
              f"pending_tasks={len(active_tasks)}")
        for t in asyncio.all_tasks():
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    print(f"  !! TASK RAISED: {t.get_name()}: {exc!r}")

    orch.operator_stop()
    await asyncio.sleep(2.0)
    print(f"State after stop: {orch.state}  current_run={orch._current_run}")

    orch.operator_abort()
    for _ in range(50):   # up to 5s — abort's own handler includes a 2s mock robot park
        if orch._current_run is None:
            break
        await asyncio.sleep(0.1)
    print(f"State after abort: {orch.state}  current_run={orch._current_run}  storage={orch._storage}")

    orch._running = False
    if run_task.done():
        exc = run_task.exception()
        if exc is not None:
            print(f"\n!!! orch.run() TASK CRASHED: {exc!r}")
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    else:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    # ── Read back what actually landed on disk ─────────────────────────
    db_path = OUT_DIR / "picks.db"
    print(f"\n{'='*70}\nDB file exists: {db_path.exists()}  size={db_path.stat().st_size if db_path.exists() else 0} bytes")
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        runs = conn.execute("SELECT * FROM runs").fetchall()
        picks = conn.execute("SELECT * FROM picks").fetchall()
        print(f"\nruns table ({len(runs)} rows):")
        for r in runs:
            print(f"  {dict(r)}")
        print(f"\npicks table ({len(picks)} rows):")
        for p in picks:
            print(f"  {dict(p)}")
        conn.close()

    runs_dir = OUT_DIR / "runs"
    csv_files = list(runs_dir.rglob("*.csv")) if runs_dir.exists() else []
    print(f"\nCSV files found: {len(csv_files)}")
    for f in csv_files:
        print(f"  {f}")
        print("  " + f.read_text().replace("\n", "\n  "))


if __name__ == "__main__":
    asyncio.run(main())
