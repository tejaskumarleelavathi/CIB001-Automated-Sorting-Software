"""Minimal isolation: does the queue process a second event after the first, at all?"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cib.core.events import OperatorAbortEvent, OperatorStartEvent
from cib.core.orchestrator import DriverSet, Orchestrator
from cib.core.state_machine import State
from cib.drivers.camera.mock import MockCameraDriver
from cib.drivers.lighting.mock import MockLightingDriver
from cib.drivers.plc.mock import MockPLCDriver
from cib.drivers.pump.mock import MockPumpDriver
from cib.drivers.robot.mock import MockRobotDriver
from cib.drivers.safety.mock import MockSafetyDriver

OUT_DIR = Path(os.environ.get("TEMP", r"C:\Temp")) / f"cib_min_{int(time.time())}"


async def main() -> None:
    OUT_DIR.mkdir(parents=True)
    drivers = DriverSet(
        robot=MockRobotDriver(), safety=MockSafetyDriver(), plc=MockPLCDriver(),
        pump=MockPumpDriver(),
        camera_overhead=MockCameraDriver(name="overhead", num_fake_samples=1),
        camera_eoa=MockCameraDriver(name="eoa", num_fake_samples=1),
        lighting=MockLightingDriver(),
    )
    orch = Orchestrator(drivers=drivers, db_path=OUT_DIR / "picks.db", runs_base_dir=OUT_DIR / "runs")

    run_task = asyncio.create_task(orch.run())
    for _ in range(50):
        if orch.state == State.IDLE:
            break
        await asyncio.sleep(0.1)
    print(f"1) after startup: {orch.state}")

    orch.operator_start()
    await asyncio.sleep(1.0)
    print(f"2) after start: {orch.state}  current_run={orch._current_run.run_id if orch._current_run else None}")

    orch.operator_abort()
    await asyncio.sleep(1.0)
    print(f"3) after abort (no stop in between): {orch.state}  current_run={orch._current_run}")

    orch._running = False
    if not run_task.done():
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
    else:
        exc = run_task.exception()
        if exc:
            print(f"CRASHED: {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())
