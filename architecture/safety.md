# Safety Architecture — Sick Flexi Compact

Last updated: 2026-07-02

---

## Hardware

| Parameter        | Value |
|-----------------|-------|
| Model           | Sick Flexi Compact safety controller |
| IP              | 172.16.9.12 |
| Port            | 502 (ModbusTCP) |
| Protocol        | ModbusTCP, FC4 (read input registers), FC6 (write single holding register) |
| Process I/O     | Input register address **256**, read 25 registers |
| Device metadata | Input register address 2000, 1 register — must read 0x0003 (Run mode) on connect |
| Driver file     | `cib/drivers/safety/driver.py` — class `FlexiCompactDriver` |
| Test script     | `hardware_tests/test_safety_driver.py` |

---

## Bit Map — Register 256 (CONFIRMED 2026-07-02)

All bits use **normally-closed convention**: **1 = OK/safe, 0 = fault/open**.

| Bit | Signal | 1 means | 0 means |
|-----|--------|---------|---------|
| 0   | E-stop button OK | button not pressed | **button pressed** |
| 1   | Unknown (spare) | always 1 in tests | — |
| 2   | Light curtain OK | beam intact / safe | **beam broken (tripped)** |
| 3   | Reset button | being pressed | not pressed |
| 4   | Unknown (spare) | always 1 in tests | — |
| 8   | Safety contactor | **closed / running** | open (e-stop fired) |
| 9   | Reset available | e-stop released, awaiting reset | — |
| 10  | Safety output OK | all safe | **any fault latched** |
| 11  | Transient reset | appears briefly during reset | — |
| 15  | Heartbeat | toggles at 1Hz | — (masked out in driver) |

**Baseline (all safe):** `reg256 = 0x0517` (bits 0, 1, 2, 4, 8, 10 = 1)

### State transitions observed during rawwatch session

| Event | Register value | Notes |
|-------|---------------|-------|
| Normal (all safe) | `0x0517` | bits 0,1,2,4,8,10 = 1 |
| E-stop pressed | `0x0016` | bits 0, 8, 10 lost (button down, contactor open) |
| E-stop released | `0x0217` | bit 0 restored, bit 9 set (awaiting blue reset) |
| Blue reset pressed | `0x001F` | bit 3 set, bit 9 cleared |
| Contactor re-closes | `0x0917` | bit 8 restored |
| Back to normal | `0x0517` | bit 10 restored |
| Light curtain broken | `0x0113` | bits 2, 10 lost; **bit 8 stays** (contactor stays closed) |
| Light curtain clear | `0x0917` | bit 2 restored; bit 10 still 0 (latched until reset-ack) |

---

## E-stop vs Protective Stop — Key Difference

| | E-stop | Protective Stop (light curtain) |
|--|--------|--------------------------------|
| Contactor (bit 8) | **OPEN** — motor power cut | **CLOSED** — motor power stays on |
| Robot motion | Immediately killed by hardware | Software halt sent (robot decelerates) |
| Reset sequence | Physical blue button on Flexi panel | Software `send_reset_acknowledge()` write to Flexi register (or physical blue button) |
| State machine state | `E_STOP` | `PROTECTIVE_STOP` |
| Resume path | Operator presses CONTINUE on HMI after physically pressing blue button | Operator presses CONTINUE on HMI (after beam clears) |

---

## Driver Implementation

**File:** `cib/drivers/safety/driver.py`

### What's implemented

- `connect()` — TCP connect to 172.16.9.12:502, verifies device status = 0x0003 (Run mode)
- `read_status()` — FC4 read of 25 input registers at address 256, returns decoded dict
- `disconnect()` — closes TCP socket cleanly
- `send_reset_acknowledge()` — **no-op until `RESET_ACK_REG` is confirmed (OQ-23)**

### `read_status()` return dict

```python
{
    "any_fault_active":      bool,   # True when bit 10 is low (any fault latched)
    "light_curtain_tripped": bool,   # True when bit 2 is low (beam broken now)
    "estop_active":          bool,   # True when contactor open AND light curtain fine
    "estop_button_index":    int | None,  # 0 = the one confirmed button
    "contactor_closed":      bool,   # True when bit 8 high
    "reset_available":       bool,   # True when bit 9 high (blue light flashing)
}
```

### Detection logic (inside `_read_status_sync`)

```python
w = regs[0] & ~HEARTBEAT_MASK      # mask bit 15 before decode

light_curtain_tripped = not bit(2)  # beam broken = bit 2 low
estop_active = (not bit(8)) and bit(2)  # contactor open AND light curtain fine
any_fault_active = not bit(10)      # safety output latched low
```

---

## Orchestrator Integration

The orchestrator polls the Flexi every **100ms** via `_safety_poll_loop()`.

On every poll, `_light_curtain_tripped` is updated live on the orchestrator instance. This gates the CONTINUE button: if the beam is still broken when the operator presses CONTINUE, the resume is blocked with a warning.

### Safety event flow

```
SafetyStatusEvent (100ms)
  → _evaluate_safety()
      → if estop_active:
            robot.halt()
            sm.safety_fault_estop()    → State.E_STOP
            plc.set_status_light(RED)
      → elif light_curtain_tripped:
            robot.halt()
            sm.safety_fault_protective() → State.PROTECTIVE_STOP
            plc.set_status_light(AMBER)
```

### Resume flow

**E-stop resume (OperatorAcknowledgeEvent in E_STOP state):**
```
Operator physically resets e-stop → presses blue button → contactor re-closes
  → Operator presses CONTINUE on HMI
    → _safe_recovery() — home robot, stop streams, ring light off
    → sm.hardware_reset_complete()  → State.IDLE
    → plc.set_status_light(AMBER)
    → Operator presses START for next cycle
```

**Protective stop resume (OperatorAcknowledgeEvent in PROTECTIVE_STOP state):**
```
Operator clears light curtain beam
  → Operator presses CONTINUE on HMI
    → if _light_curtain_tripped: BLOCKED — warn, do nothing
    → else:
        safety.send_reset_acknowledge()  ← releases latch (OQ-23 pending)
        _safe_recovery()  — home robot, stop streams
        sm.start()  → State.RUNNING
        plc.set_status_light(GREEN)
        _run_cycle() kicked off
```

---

## Safe Recovery (`_safe_recovery()`)

Called in **both** acknowledge handlers before resuming. Defined in `cib/core/orchestrator.py`.

**Steps:**
1. Waits up to 10s for `_cycle_active` to drop to False (cycle task exits after robot halt)
2. Stops overhead and EoA camera streams (best-effort)
3. Turns ring light off (best-effort)
4. Calls `robot.move_to_safe_park()` — sort_server.prg moves **Z to SAFE (-47.700mm) first**, then XY home. Pipette tip retracts before any horizontal movement.
5. Resets `_cycle_phase` to `IDLE`

**CyclePhase tracking:** The orchestrator sets `self._cycle_phase` at every step of `_run_cycle()` so recovery logs exactly where the fault hit:

| Phase | When set |
|-------|---------|
| `OVERHEAD_CAPTURE` | Cycle starts, overhead streaming |
| `OVERHEAD_VISION` | Overhead blob detection running |
| `ROBOT_MOVING` | Robot travelling to EoA image site |
| `EOA_CAPTURE` | EoA camera streaming, frames being captured |
| `EOA_VISION` | EoA classifier running |
| `ROBOT_PARKING` | Robot returning to safe park |
| `IDLE` | Between cycles, or after recovery |

Future pick-place phases (not yet implemented — add phase checks in `_safe_recovery()` when wired in):

| Phase | When set | Recovery note |
|-------|---------|---------------|
| `ROBOT_PICK` | Robot lowering pipette tip to specimen | HOME is still safe (Z up first) |
| `PUMP_ASPIRATING` | Pump pulling specimen into tip | **Bug may be in tip** — consider dispense-in-place before homing |
| `ROBOT_TO_WELL` | Robot carrying specimen to well | Bug in tip — dispense-in-place or accept loss |
| `PUMP_DISPENSING` | Pump dispensing into well | Bug going into well — probably fine to abort |

---

## Open Questions (must resolve before full safety handoff)

### OQ-23 — Light curtain latch release register (BLOCKING)

`RESET_ACK_REG` in `cib/drivers/safety/driver.py` is currently `None`.

```python
RESET_ACK_REG: int | None = None   # ← set this once David confirms
```

Until confirmed, `send_reset_acknowledge()` is a **no-op**. The light curtain latch can only be released by the **physical blue button** on the Flexi panel.

**What to ask David:** "Which Modbus holding register (FC6 write) on the Flexi Compact triggers the light curtain latch release? I need to do a 100ms pulse: write 1, sleep 0.1s, write 0."

**When you have the address:** update `RESET_ACK_REG = <address>` in `driver.py`. The `_pulse_reset_sync()` method is already written and will activate automatically.

---

## Test Script

```
python hardware_tests/test_safety_driver.py
```

Prints one line per state change (change-on-delta only). Ctrl+C to stop.

Output columns: `TIME | STATUS | ESTOP | LIGHT CURTAIN | CONTACTOR | RESET AVAIL`

Verified states:
- Normal: `OK  |  no  |  clear  |  CLOSED  |  no`
- E-stop pressed: `E-STOP ACTIVE  |  YES  |  clear  |  OPEN  |  no`
- E-stop released: `FAULT (latched)  |  no  |  clear  |  OPEN  |  YES`
- Blue button pressed → back to: `OK  |  no  |  clear  |  CLOSED  |  no`
- Light curtain broken: `LIGHT CURTAIN  |  no  |  TRIPPED  |  CLOSED  |  no`
- Light curtain clear (latched): `FAULT (latched)  |  no  |  clear  |  CLOSED  |  no`
