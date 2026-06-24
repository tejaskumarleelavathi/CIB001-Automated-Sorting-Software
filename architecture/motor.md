# Motor Architecture — Novanta Liberty MDrive (LMDAE572C)

Last updated: 2026-06-18

---

## Hardware

| Parameter        | Value |
|-----------------|-------|
| Model           | Novanta LMDAE572C / LMDAM 7.008, Hardware 2.10 |
| Type            | Liberty MDrive — NEMA 23/34, hMTechnology closed-loop stepper |
| IP              | 172.16.9.16 |
| Port            | **503** (MCode/TCP — raw ASCII) |
| Steps/rev       | 200 full steps × 256 microsteps = **51,200 steps/revolution** |
| Protocol        | Plain ASCII text + CR (`\r`) over raw TCP socket |
| Manual          | `Software/MCode-V2.06-NOVA.pdf` |
| Safety interlock | Motor power cut by Sick Flexi safety contactor on e-stop/light curtain — software cannot override |

---

## How to Connect

Raw TCP socket — no Modbus, no Telnet, no special library needed.

```python
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
sock.settimeout(5.0)
sock.connect(("172.16.9.16", 503))
```

**Protocol rules:**
- Every command = ASCII text + `\r` (carriage return)
- Motor echoes the command back, then sends response data (if any), then a prompt
- `>` prompt = ready, no error
- `?` prompt = error active — read `PR ER` to find out what, clear with `ER=0`
- Read loop: accumulate bytes until `>` or `?` appears — do NOT assume instant response

**Sanity check on connect:**
```python
sock.sendall(b"PR VR\r")
# Response: 'PR VR\r\nLMDAM 7.008, Hw: 2.10\r\n>'
```

---

## Key Registers / Variables

| Variable | Read       | Write      | Meaning |
|----------|-----------|------------|---------|
| `ER`     | `PR ER`   | `ER=0`     | Error register. Non-zero = fault active. Clear with `ER=0`. |
| `DE`     | `PR DE`   | `DE=1`     | Drive enable. 0 = bridge disabled (after stall). Set `DE=1` to re-enable. |
| `MV`     | `PR MV`   | —          | Moving flag. 1 = in motion, 0 = stopped. |
| `P`      | `PR P`    | `P=0`      | Position counter (steps, signed). `P=0` redefines current position as zero. |
| `LK`     | `PR LK`   | —          | Program lock. 1 = programs locked (clear with `CP` + `S`). |
| `VR`     | `PR VR`   | —          | Firmware version string. |

---

## Common Error Codes

| ER  | Meaning | Fix |
|-----|---------|-----|
| 0   | No error | — |
| 24  | Illegal Data Entered — bad syntax or reserved label name | Fix syntax; use `A*` label prefix |
| 28  | Trying to redefine an existing program label | `CP` + `S` to clear, then re-upload |
| 44  | Program locked | `CP` + `S` to unlock |
| 45  | Program space overflow | `CP` + `S`, reduce program size |
| 46  | Not in Program Mode — sent `LB`/`MR`/etc. outside `PG` | Enter program mode first with `PG <addr>` |
| 64  | Label already exists | `CP` + `S` to clear, then re-upload |
| 86  | Stall detected — drive bridge disabled | `ER=0` + `DE=1`; power cycle if persists |
| 104 | hMT Locked Rotor | Power cycle required |

---

## Startup / Recovery Sequence

Always run this before any motion command or program upload:

```python
sock.sendall(b"ER=0\r")   # clear error register
sock.sendall(b"DE=1\r")   # re-enable drive bridge (disabled after stall)
time.sleep(0.5)            # let drive stabilise
# Verify:
sock.sendall(b"PR ER\r")  # must return 0
sock.sendall(b"PR DE\r")  # must return 1
```

---

## Immediate Mode Motion Commands

These execute instantly — motor moves while Python waits and polls `PR MV`.

```
MR <steps>    Relative move.  MR 51200 = 1 rev CW.  MR -51200 = 1 rev CCW.
MA <pos>      Absolute move.  MA 0 = return to position 0.
SL 0          Stop (slew velocity to 0).  Use as emergency stop.
H             Hold — blocks until current motion is complete (program mode only).
```

**Polling pattern (immediate mode):**
```python
sock.sendall(b"MR 51200\r")
# ... wait for '>' ...
while True:
    sock.sendall(b"PR MV\r")
    # parse response — if "0" then stopped
    if mv == "0":
        break
    time.sleep(0.2)
```

---

## Flashing Programs to Motor

Programs are stored in the motor's non-volatile flash and survive power cycles.
Python only needs to send `EX <label>\r` to trigger them.

### Step-by-step upload flow

```
1.  ER=0          clear any error (immediate mode)
2.  DE=1          enable drive (immediate mode)
3.  CP            clear ALL existing programs from flash
4.  S             save the cleared state to flash
5.  PG 1          enter program mode at address 1
                  ← from here, motor responds with address numbers, NOT '>'
6.  LB A1         define program label "A1"
7.  MR 51200      motion instruction
8.  H             hold until motion complete (REQUIRED inside programs)
9.  E             end program
    ... repeat LB/body/H/E for each program ...
10. PG            exit program mode (back to immediate mode, '>' returns)
11. S             save all programs to flash
```

### Protocol difference: immediate mode vs program mode

| | Immediate mode | Program mode |
|---|---|---|
| Enter with | (default on connect) | `PG <address>` |
| Exit with | — | `PG` (no address) |
| Success prompt | `>` | bare address number e.g. `  6` |
| Error prompt | `?` | `?` |
| Commands available | PR, EX, MR, MA, ER=, DE=, CP, S, PG | LB, MR, MA, H, E, BR, CL, RT |

**Critical:** `LB`, `H`, `E` only work inside program mode. Sending them in immediate mode causes **error 46 "Not in Program Mode"**.

### Label name rules (LMDAM 7.008)

- Exactly 2 characters
- First character: **letter A–Z** (some letters are reserved — see below)
- Second character: letter A–Z or digit 0–9
- Special label `SU` auto-executes on power-up — do not use
- **Confirmed reserved first-letter prefixes** (trigger ER=24): `F`
- **Confirmed working prefix**: `A` — use `A1`, `A2`, `A3`, etc.

---

## Programs Currently on Motor

Flashed 2026-06-18. These survive power cycles and are ready to call.

| Label | MCode body | Purpose |
|-------|-----------|---------|
| `A1`  | `MR 51200` + `H` + `E` | Clockwise 1 revolution (aspirate direction) |
| `A2`  | `MR -51200` + `H` + `E` | Counter-clockwise 1 revolution (dispense direction) |
| `A3`  | `MA 0` + `H` + `E` | Return to absolute position 0 (home) |

Steps per revolution: **51,200** (200 full steps × 256 microsteps MS=256).

> When syringe pump mechanics are confirmed, update `STEPS_PER_MOVE` in
> `hardware_tests/mdrive_control.py` and re-run `upload` to re-flash.

---

## Calling Programs from Python

```python
# Trigger clockwise (aspirate):
sock.sendall(b"EX A1\r")
# wait for '>' prompt — motor runs program autonomously
# then poll until stopped:
while True:
    sock.sendall(b"PR MV\r")
    if mv == "0":
        break
    time.sleep(0.2)

# Trigger counter-clockwise (dispense):
sock.sendall(b"EX A2\r")
# ... same poll pattern ...

# Return to home:
sock.sendall(b"EX A3\r")
# ... same poll pattern ...
```

The `H` instruction inside each program means `EX` will not return `>` until
the move is physically complete. `PR MV` will almost always already be 0 on
the first poll — the polling loop is a safety net.

---

## Test Scripts — Rotary Motor (hardware_tests/)

| Script | Purpose |
|--------|---------|
| `test_mdrive.py` | Basic connection test: PR VR, clear ER, MR forward + reverse, poll MV, PR P |
| `mdrive_control.py upload` | Flash programs A1/A2/A3 to motor (run once, or after STEPS_PER_MOVE change) |
| `mdrive_control.py run` | Interactive: type `cw`, `ccw`, `home`, `pos`, `zero` at prompt |
| `mdrive_control.py cw\|ccw\|home\|pos` | One-shot single command |
| `mdrive_probe_pgm.py` | Diagnostic: tests program-mode write + label name compatibility |

**Run with:**
```
C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe hardware_tests\mdrive_control.py upload
```

---

## Integration into Pipeline — Rotary Motor (future)

When integrating into `cib/drivers/pump/driver.py`:

```python
class MDriveDriver(PumpProtocol):
    ASPIRATE_CMD  = b"EX A1\r"
    DISPENSE_CMD  = b"EX A2\r"
    HOME_CMD      = b"EX A3\r"

    async def aspirate(self) -> None:
        await self._send(self.ASPIRATE_CMD)
        await self._wait_for_stop()

    async def dispense(self) -> None:
        await self._send(self.DISPENSE_CMD)
        await self._wait_for_stop()
```

Steps per move (`A1`/`A2`) must be tuned to syringe pump mechanics once the
physical syringe assembly is confirmed (volume per revolution, stroke length).

---
---

# Linear Actuator — Novanta LMDAE572C (Syringe Pump)

Last updated: 2026-06-18

---

## Hardware

| Parameter        | Value |
|-----------------|-------|
| Model           | Novanta LMDAE572C (Liberty MDrive — linear actuator variant) |
| Type            | External threaded lead screw — carriage moves linearly along screw |
| IP              | 172.16.9.60 |
| Port            | **503** (same MCode/TCP protocol as rotary motor) |
| Step counts     | ±50,000 steps confirmed safe by live manual jogging — do NOT change without re-testing |
| Lead screw pitch | **Unknown** — work in raw steps only until pitch confirmed with Weifeng |
| Manual          | `Software/MCode-V2.06-NOVA.pdf` (same as rotary motor) |

**Same protocol as rotary motor** — everything in the Connect / Protocol / Error Codes / Program Mode sections above applies identically. Only the IP, step counts, and label names differ.

---

## Programs Currently on Motor

Flashed 2026-06-18. Survive power cycles.

| Label | MCode body | Purpose |
|-------|-----------|---------|
| `G1`  | `MR -50000` + `H` + `E` | Drop / dispense — carriage moves forward |
| `G2`  | `MR +50000` + `H` + `E` | Aspire / draw — carriage moves backward |

Labels use `G*` prefix — confirmed safe in LMDAE firmware.
Direction was confirmed and corrected during testing (initial assignment was reversed).

> **To re-flash** after changing step counts: update `DROP_STEPS` / `ASPIRE_STEPS`
> in `hardware_tests/linear_control.py` then re-run `upload`.

---

## Test Scripts — Linear Actuator (hardware_tests/)

| Script | Purpose |
|--------|---------|
| `linear_control.py upload` | Flash G1/G2 to linear motor (run once, or after step count change) |
| `linear_control.py run` | Interactive: type `drop`, `aspire`, `pos`, `zero` at prompt |
| `linear_control.py drop\|aspire\|pos` | One-shot single command |

**Run with:**
```
C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe hardware_tests\linear_control.py upload
C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe hardware_tests\linear_control.py run
```

---

## How Aspire/Drop Fit into the Pipeline

Per PRD (FR-PUMP-02, FR-PUMP-03, FR-ROB-06), **Python commands the motor**,
triggered by signals from the robot. The robot does not talk to the motor directly.

```
Robot arrives at pick position
  → sends ROBOT_AT_PICK_POSITION signal to Python orchestrator
    → Python calls pump_driver.aspirate()
      → sends EX G2\r to 172.16.9.60:503
        → motor draws back (specimen sucked up)
          → Python receives > prompt → ASPIRATE_COMPLETE

Robot arrives at place position (well)
  → sends ROBOT_AT_PLACE_POSITION signal to Python orchestrator
    → Python calls pump_driver.dispense()
      → sends EX G1\r to 172.16.9.60:503
        → motor pushes forward (specimen expelled into well)
          → Python receives > prompt → DISPENSE_COMPLETE
```

**Note:** The robot *could* command the motor directly via SPEL+ `SockOpen`/`SockSend`
(TCP socket functions exist in SPEL+). This was considered but rejected because:
- Python needs to know about pump faults to trigger cycle stop (FR-PUMP-04/05)
- Every aspirate/dispense must be logged to the data layer
- One orchestrator = one place for error handling and state machine updates

---

## Integration into Pipeline (implement in `cib/drivers/pump/driver.py`)

```python
import socket
import asyncio
import time
from cib.drivers.base import PumpProtocol

class LinearPumpDriver(PumpProtocol):
    """
    Driver for Novanta LMDAE572C linear actuator at 172.16.9.60:503.
    Programs G1 (drop) and G2 (aspire) must be pre-flashed to the motor
    using hardware_tests/linear_control.py upload before this driver is used.
    """

    ASPIRATE_CMD = b"EX G2\r"   # carriage draws back — sucks up specimen
    DISPENSE_CMD = b"EX G1\r"   # carriage pushes forward — expels into well

    POLL_INTERVAL_S = 0.25
    MOVE_TIMEOUT_S  = 60.0

    def __init__(self, ip: str, port: int) -> None:
        self._ip   = ip
        self._port = port
        self._sock: socket.socket | None = None

    async def connect(self) -> None:
        """Open TCP socket, verify firmware, clear errors, enable drive."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(5.0)
        self._sock.connect((self._ip, self._port))
        self._send_imm("ER=0")   # clear any startup error
        self._send_imm("DE=1")   # ensure drive is enabled
        time.sleep(0.2)

    async def aspirate(self) -> None:
        """Command G2 — carriage draws back, specimen sucked into pipette."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._run_program, self.ASPIRATE_CMD)

    async def dispense(self) -> None:
        """Command G1 — carriage pushes forward, specimen expelled into well."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._run_program, self.DISPENSE_CMD)

    def _run_program(self, cmd: bytes) -> None:
        self._sock.sendall(cmd)
        self._read_prompt()      # wait for '>' — motor blocks until H completes
        self._wait_for_stop()   # safety net poll in case H didn't fully block

    def _wait_for_stop(self) -> None:
        deadline = time.monotonic() + self.MOVE_TIMEOUT_S
        while time.monotonic() < deadline:
            self._sock.sendall(b"PR MV\r")
            resp = self._read_prompt()
            if "0" in resp.splitlines():
                return
            time.sleep(self.POLL_INTERVAL_S)
        self._sock.sendall(b"SL 0\r")
        raise RuntimeError("Linear pump move timeout")

    def _send_imm(self, cmd: str) -> str:
        self._sock.sendall((cmd + "\r").encode("ascii"))
        return self._read_prompt()

    def _read_prompt(self) -> str:
        buf = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                buf += self._sock.recv(256).decode("ascii", errors="replace")
                if ">" in buf or "?" in buf:
                    break
            except socket.timeout:
                continue
        if "?" in buf:
            raise RuntimeError(f"Motor error prompt received: {buf!r}")
        return buf.strip()

    async def disconnect(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None
```

**Wire into orchestrator** (`cib/core/orchestrator.py`):
```python
# On ROBOT_AT_PICK_POSITION event:
await self._pump.aspirate()
self._event_queue.put_nowait(AspirateCompleteEvent())

# On ROBOT_AT_PLACE_POSITION event:
await self._pump.dispense()
self._event_queue.put_nowait(DispenseCompleteEvent())
```

**Machine config** (`config/machine_config.yaml`):
```yaml
pump:
  ip: "172.16.9.60"
  port: 503
  aspirate_label: "G2"   # MR +50000 — draws back
  dispense_label: "G1"   # MR -50000 — pushes forward
  move_timeout_s: 60
```

> **Before first run:** confirm lead screw pitch with Weifeng, measure steps-per-mm,
> update step counts in `hardware_tests/linear_control.py`, re-flash with `upload`.
Re-flash motor with updated step count via `mdrive_control.py upload`.
