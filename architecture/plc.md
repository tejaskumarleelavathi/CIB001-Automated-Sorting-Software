# PLC Architecture — AutomationDirect Logic P1-622

Last updated: 2026-07-02

---

## Hardware

| Parameter        | Value |
|-----------------|-------|
| Model           | AutomationDirect Logic P1-622 PLC |
| IP              | 172.16.9.11 |
| Port            | 502 (ModbusTCP) |
| Protocol        | ModbusTCP, FC3 (read holding registers), FC6 (write single holding register), FC5 (write single coil) |
| Driver file     | `cib/drivers/plc/driver.py` — class `LogicPLCDriver` |
| Test script     | `hardware_tests/test_status_light.py` |

---

## Status Light — Register Map (CONFIRMED 2026-06-18)

Holding registers (0-based offset → Modbus address 40001 = offset 0):

| Offset | Modbus | Name | Values |
|--------|--------|------|--------|
| 0 | 40001 | Mode | 0=Off 1=Steady 2=Flash 3=TwoColorFlash 4=50/50 5=Rotate 6=Chase 7=IntensitySweep 8=ColorSweep 9=Sequence 10=Wave 11=DoubleWave |
| 1 | 40002 | Animation Pattern | — |
| 2 | 40003 | Animation Direction | 0=CCW 1=CW |
| 3 | 40004 | Animation Speed | 0=Slow 1=Medium 2=Fast |
| 4 | 40005 | Sequence Value | — |
| 5 | 40006 | Primary Color | 0=Green 1=Red 2=Orange 3=Amber 4=Yellow 5=LimeGreen 6=SpringGreen 7=Cyan 8=SkyBlue 9=Blue 10=Violet 11=Magenta 12=Rose 13=White |
| 6 | 40007 | Primary Intensity | 0=High 1=Medium 2=Low 3=N/A 4=Off |
| 7 | 40008 | Secondary Color | (same as Primary Color) |
| 8 | 40009 | Secondary Intensity | (same as Primary Intensity) |

**Coil:**

| Coil | Modbus | Name | Values |
|------|--------|------|--------|
| 0 | 00001 | Demo Mode | 0=off 1=on (turn off after testing) |

---

## Status Light States Used by the System

| Colour | Mode | Primary | Meaning |
|--------|------|---------|---------|
| `green` | Steady | Green | System running normally |
| `amber` | Steady | Amber | Paused / stopped / waiting for operator |
| `red` | Flash | Red | Fault / e-stop / connection loss |
| `off` | Off | — | All off |

Called via `plc.set_status_light(StatusLight.GREEN / AMBER / RED / OFF)`.

### When each colour is set

| Colour | When |
|--------|------|
| GREEN | All devices ready (startup), operator presses START, protective stop resumed |
| AMBER | Operator presses STOP, abort, e-stop acknowledge (waiting for next START), graceful stop |
| RED | Any fault — e-stop detected, connection loss |
| OFF | — |

---

## Driver Implementation

**File:** `cib/drivers/plc/driver.py`

### What's implemented

- `connect()` — TCP connect to 172.16.9.11:502
- `disconnect()` — closes TCP socket
- `set_status_light(colour)` — writes 6 registers in sequence: mode, primary color, primary intensity, secondary color, secondary intensity, speed
- `set_work_light(on)` — **no-op, logs warning** (OQ-11 pending)
- `read_event_log()` — **returns `[]`** (OQ-11 pending)

All Modbus I/O runs via `asyncio.run_in_executor` — never blocks the event loop.

### `set_status_light()` register writes

The driver writes these 6 registers in one pass (independent, no dependency between them):

```python
for addr, val in [
    (REG_MODE,       mode),
    (REG_COLOR_PRI,  pri_col),
    (REG_INTENS_PRI, pri_int),
    (REG_COLOR_SEC,  sec_col),
    (REG_INTENS_SEC, pri_int),   # match intensity on both channels
    (REG_SPEED,      speed),
]:
    c.write_register(address=addr, value=val)
```

---

## Orchestrator Integration

PLC is polled every **100ms** via `_plc_poll_loop()` for event log entries (currently returns `[]` until OQ-11 is resolved).

Status light is commanded directly by the orchestrator at each state transition — never polled, always written on change.

### Status light command points in orchestrator

| Location | Colour |
|----------|--------|
| `AllDevicesReadyEvent` | GREEN |
| `OperatorStartEvent` | GREEN |
| `OperatorStopEvent` | AMBER |
| `OperatorAbortEvent` | AMBER |
| `OperatorAcknowledgeEvent` (e-stop path, after recovery) | AMBER |
| `OperatorAcknowledgeEvent` (protective stop path, after recovery) | GREEN |
| `SafetyStatusEvent` → estop detected | RED |
| `ConnectionLostEvent` | RED |
| `_finish_stop()` (graceful stop complete) | AMBER |

---

## Open Questions (must resolve with David before OQ-11 is closed)

### OQ-11 — Work light register address (PENDING)

`set_work_light()` is a no-op. The work light illuminates the specimen tray area for human use.

**What to ask David:** "Which Modbus holding register (FC6 write) controls the work light on/off? Single register, 0=off 1=on is typical."

**When you have the address:** replace the `log.warning` stub in `set_work_light()` with:
```python
async def set_work_light(self, on: bool) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, self._write_work_light_sync, on)

def _write_work_light_sync(self, on: bool) -> None:
    r = self._client.write_register(address=WORK_LIGHT_REG, value=1 if on else 0)
    if r.isError():
        log.error("plc.set_work_light.error", on=on, error=str(r))
    else:
        log.debug("plc.work_light", on=on)
```

Add `WORK_LIGHT_REG = <address>` as a constant at the top of `driver.py`.

### OQ-11 — Event log register address (PENDING)

`read_event_log()` returns `[]`. The PLC event log holds fault codes and operator actions.

**What to ask David:** "Which Modbus holding registers hold the PLC event log? How many registers, and what is the encoding (integer fault codes, ASCII strings, etc.)?"

**When you have the map:** replace the stub in `read_event_log()` with actual FC3 reads and decode the entries into strings.

---

## Work Light — When to Use (future)

Once OQ-11 is resolved, the work light should be:

| State | Work light |
|-------|-----------|
| System running (RUNNING) | OFF — robot is moving, keep humans out |
| System stopped / IDLE | ON — safe for humans to access the specimen tray |
| E-stop / protective stop | ON — human intervention needed |
| Startup / shutdown | ON |

Wire into orchestrator at the same state transition points as the status light. Add to `_finish_stop()`, `OperatorAbortEvent`, `OperatorAcknowledgeEvent`, and `AllDevicesReadyEvent` handlers.

---

## Test Script

```
python hardware_tests/test_status_light.py
```

Cycles through green → amber → red → off on the tower light. Verified 2026-06-18.
