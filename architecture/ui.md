# UI — Architecture & Operator Guide

**Date written:** 2026-06-19  
**Status:** Phase 1 placeholder UI in place. Full HMI UI deferred to Phase 2.

---

## Threading model

```
Main thread     PyQt6 event loop (QApplication.exec())
                └── MainWindow polls orchestrator.state every 500ms (QTimer)
                └── Button clicks call orchestrator.operator_*() — thread-safe

Daemon thread   asyncio event loop
                └── orchestrator.run()
                     ├── _startup()        connects all devices, turns on backlight
                     ├── _safety_poll_loop  polls Flexi every 100ms
                     ├── _plc_poll_loop     polls PLC every 100ms
                     └── _event_loop        processes operator commands + device events
```

All `orchestrator.operator_*()` calls use `loop.call_soon_threadsafe()` internally — safe to call from Qt thread.

---

## Buttons and states

| Button   | Enabled when              | What it does |
|---|---|---|
| START    | IDLE                      | Turns backlight on (waits for stability), starts pick-place cycle loop |
| STOP     | RUNNING, CYCLE_STOPPING   | Marks cycle for graceful stop — current cycle finishes, then backlight off |
| SHUTDOWN | IDLE, FAULT               | Disconnects all hardware cleanly, exits the program |

**Why STOP ≠ SHUTDOWN:**  
STOP finishes the current cycle and returns to IDLE. The program keeps running. You can click START again.  
SHUTDOWN ends the session: sends `DISCONNECT` to the lighting controller, stops the asyncio loop, closes the window.

---

## Shutdown sequence — how it works

When the operator clicks **SHUTDOWN**:

1. `_on_shutdown()` in `MainWindow` disables the SHUTDOWN button immediately (prevents double-click)
2. `orchestrator.operator_shutdown()` queues an `OperatorShutdownEvent` on the asyncio event loop
3. A 3-second `QTimer` fires → `QApplication.quit()` closes the Qt window
4. In the asyncio thread, `_handle_event` processes `OperatorShutdownEvent`:
   - Sends `SET:MODE:CHANNEL{1,2,3},0` → backlight off
   - Sends `DISCONNECT;` → DCS-100E closes its TCP server session cleanly
   - Sets `self._running = False` → all poll loops exit on next iteration (≤100ms)
5. `orchestrator.run()` returns → asyncio thread exits
6. `main.py` sees the thread has exited → skips the force-stop path

**Result:** on next startup, the DCS-100E accepts the new connection immediately. No WinError 10054.

---

## What happens if you close the window without SHUTDOWN (X button or Ctrl+C)

`main.py` detects the asyncio thread is still alive after 4 seconds and calls `orchestrator.operator_shutdown()` automatically. This gives the asyncio loop up to 3 more seconds to disconnect cleanly. If it still doesn't exit (e.g. camera is hanging), the loop is force-stopped.

In this case, the DCS-100E TCP session is abruptly terminated. On next startup, the lighting driver retries the connection up to 3 times with 1.5s backoff — the device typically clears the stale session within 1.5s. If all 3 retries fail, startup faults with a clear error.

**Best practice: always use SHUTDOWN to exit.**

The DCS-100E only allows one TCP connection at a time. If the process was killed without DISCONNECT, the device clears its stale session automatically after ~15s — no power-cycle required. The lighting driver retries for up to 30s (10 attempts × 3s) to cover this. If the device is still not responding after 30s, the fault is a genuine network/hardware issue (check cable and IP), not a session cleanup issue.

---

## State badge colours

| State          | Colour  | Meaning |
|---|---|---|
| INITIALIZING   | Grey    | Connecting to all devices |
| IDLE — READY   | Green   | All devices connected, ready to run |
| RUNNING        | Blue    | Cycle loop active |
| STOPPING…      | Orange  | Finishing current cycle (STOP was pressed) |
| PROTECTIVE STOP| Orange  | Light curtain tripped — press START to resume |
| E-STOP         | Red     | E-stop button pressed — reset required |
| FAULT          | Purple  | Unrecoverable error — check logs, SHUTDOWN and restart |

---

## Phase 1 placeholder vs Phase 2 full HMI

This is the Phase 1 placeholder in `cib/ui/main_window.py`. It has the minimum needed to test the hardware pipeline.

**Phase 2 UI** (`main_window.py` will be replaced entirely — orchestrator API unchanged):

| Screen | Contents |
|---|---|
| Pre-run setup | Field code entry, plate ID, starting well selection, plate config file picker |
| Run view | Live overhead camera feed (annotated centroids), plate station map with per-well fill status |
| Safety banner | E-stop indicator, light curtain status, CONTINUE button for protective stop reset |
| EoA feed | Live EoA camera feed with classification result overlay |
| Config manager | Select/edit plate config YAML, calibration file path |
| Export status | Per-run CSV export progress, file path shown |

The orchestrator API (`operator_start`, `operator_stop`, `operator_abort`, `operator_acknowledge`, `operator_shutdown`, `state`) does not change between Phase 1 and Phase 2.

---

## Image capture counter

The counter in the bottom of the Phase 1 window counts files matching `overhead_*` in `output/overhead_images/`. It updates every 500ms on the same QTimer as the state poll. It does not count EoA images.

---

## Adding new buttons (Phase 2 guide)

1. Add a new `OperatorXxxEvent` dataclass in `cib/core/events.py`
2. Add `operator_xxx()` method to `Orchestrator` (uses `_put_event`)
3. Handle `OperatorXxxEvent` in `Orchestrator._handle_event`
4. Add button to `MainWindow` — call `orchestrator.operator_xxx()` in handler

No changes needed to the state machine unless the new button triggers a state transition.
