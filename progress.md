# CIB Software — Progress Log

Last updated: 2026-06-24 (session 5)

---

## Hardware Testing (completed before software build)

### Cameras
- [x] Lucid TRI245S-CC confirmed as overhead camera (serial 253102168, MAC 1c:0f:af:c2:3e:a0)
- [x] arena_api installed in cib_env — use direct path `C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe`
- [x] Camera 1 (overhead) at 172.16.9.21 — forced IP via force_camera_ip.py, confirmed connects and streams
- [x] With auto-exposure + backlight at 250 mA: mean ≈ 159/255 — good exposure ✅
- [x] Image captured in BGR8 format, 5320×4600 px (24.5 MP)
- [x] Camera 2 (EoA) serial=253102169, assigned IP 172.16.9.22 (force_camera_ip assigns IPs dynamically — order can vary, identify by serial)
- [x] Both cameras confirmed connecting and capturing via step0_connect_cameras.py
- [ ] Camera IPs are temporary (force_ip resets on reboot) — set persistent IPs via ArenaView GUI

### Backlight / Ring Light
- [x] DCS-100E (Advanced Illumination) at 172.16.9.20, TCP port 777, ASCII SCPI commands
- [x] Commands: `SET:MODE:CHANNEL1,1;` then `SET:LEVEL:CHANNEL1,250;` (250 mA — David confirmed, NOT 500)
- [x] `hardware_tests/test_lighting.py` — working, TIMEOUT=2.0, TCP_NODELAY, sleep=0.05
- [x] Ring light confirmed working via Epson RC+ I/O Monitor (Output 12 = ring light ON) ✅

### Robot (Epson RC90 / T3-B)
- [x] Robot IP changed from factory default 192.168.0.1 → 172.16.9.15 (stored in flash, permanent)
- [x] Robot TCP remote port: **5001** (not 5000 — was stored in controller flash)
- [x] Response format: `+,<data>` = success / `!,<code>` = error (e.g. `!,10` = E-Stop active)
- [x] `hardware_tests/robot_server.prg` — SPEL+ TCP server written and tested
- [x] Key fix: IP Address in System Config → TCP/IP → Port 201 must be set to 172.16.9.15
- [x] Output 12 = ring light ON ✅ (confirmed via RC+ I/O Monitor)
- [x] Output 4 = pipette moves (pneumatic) ✅ (confirmed via RC+ I/O Monitor)
- [ ] **BLOCKER: E-Stop circuit not wired/jumpered** — E-Stop light stays RED after all buttons released
  - Tell David: "E-Stop light stays red after releasing buttons and hitting Reset. External E-Stop terminals on RC90 aren't wired/jumpered, or Sick Flexi at .12 is sending active E-Stop signal"
  - Until fixed: all robot commands return `!,10`
- [ ] `Motor On` after e-stop not yet in SPEL+ startup sequence — needs adding
- [ ] Output 4: confirm direction — which is pipette DOWN vs UP with David
- [ ] M-Drive protocol — what commands does 172.16.9.16 accept? (OQ-14 unresolved)

### PLC (172.16.9.11 — AutomationDirect P1-622)
- [x] Modbus TCP port 502, accepts any unit ID (0, 1, 2, 255)
- [x] pymodbus 3.13.0 installed in cib_env — IMPORTANT: `count` must be keyword arg
- [x] `hardware_tests/plc_readback.py` — reads holding registers and coils to verify writes
- [x] `hardware_tests/test_status_light.py` — Modbus TCP writes confirmed sticking (reg[0]=1)
- [x] `hardware_tests/plc.py` — command line read/write tool for coils and registers
- [ ] **OQ-11 OPEN**: PLC register map — which addresses control status light, work light, event log
  - Status light not responding because register addresses are unknown — blocked on David
- [ ] RC90 remote I/O (START/STOP pins) wiring to PLC — need to ask David if wired

### Sick Flexi / Safety Controller (172.16.9.12)
- [x] `hardware_tests/probe_flexi.py` — confirmed Modbus TCP port 502
- [x] `hardware_tests/flexi_watch_raw.py` — live register change detector written
- [x] `hardware_tests/test_flexi.py` — reads input registers 2000-2001
- [ ] **OQ-23 OPEN**: Sick Flexi Modbus register map — need registers for:
  - Read: light curtain state, e-stop button states (individual), contactor state, fault type
  - Write: reset-acknowledge (light curtain latch release)
  - Write: light curtain circuit reset after e-stop recovery
- [ ] Register data frozen — David needs to check safety loop wiring

### M-Drive / Syringe Pump (172.16.9.16 — Novanta LMDAE572C)
- [x] Motor unlocked from cybersecurity lockdown via Liberty MDrive Software Suite
- [x] IP confirmed: 172.16.9.16
- [x] **OQ-14 CLOSED**: Protocol confirmed — raw TCP ASCII on **port 503** (MCode/TCP), NOT Modbus, NOT Telnet
  - Commands are plain ASCII + CR (`\r`); device echoes command then sends `>` prompt when ready
  - Error state shows `?` prompt instead of `>` until cleared
- [x] Full motor test script written: `hardware_tests/test_mdrive.py` — connect, clear ER, enable DE, MR forward/reverse, poll MV, PR P
- [x] Onboard program flash confirmed working: `hardware_tests/mdrive_control.py`
  - Programs flashed to motor non-volatile flash via `PG 1` → write MCode lines → `PG` → `S`
  - Three programs stored on motor (survive power cycles):
    - `A1` — clockwise / aspirate (MR +51200, H, E)
    - `A2` — counter-clockwise / dispense (MR -51200, H, E)
    - `A3` — return to position 0 (MA 0, H, E)
  - Python triggers with single command: `EX A1\r`, `EX A2\r`, `EX A3\r`
  - Motor executes motion autonomously; Python polls `PR MV` until 0
- [x] Interactive control tested: `mdrive_control.py run` — cw/ccw/home/pos/zero commands confirmed working
- [x] Full architecture documented: `architecture/motor.md`
- Key gotchas discovered during bring-up:
  - ER=24 on connect = "Illegal Data Entered" (NOT stall — stall is ER=86); clear with `ER=0` + `DE=1`
  - `LB` is a program-mode-only instruction — must send `PG <addr>` first or get error 46
  - In program mode motor responds with address numbers (e.g. `  6`), NOT `>` prompt
  - Label first-character `F` is reserved in LMDAM 7.008 firmware (triggers ER=24); use `A*` labels
  - `S` command commits RAM → flash; without it programs are lost on power cycle
  - MCode manual: `architecture/motor.md` or PDF at `Software/MCode-V2.06-NOVA.pdf`

### Linear Actuator / Syringe Pump (172.16.9.60 — Novanta LMDAE572C)
- [x] Device confirmed: Novanta LMDAE572C — Liberty MDrive **linear actuator** (external threaded lead screw, NOT rotary)
- [x] IP confirmed: 172.16.9.60, Port 503 (same MCode/TCP protocol as rotary motor)
- [x] Same protocol as rotary motor — raw TCP ASCII + CR, `>` / `?` prompts, program mode with `PG`
- [x] Test + control script written: `hardware_tests/linear_control.py`
- [x] Programs flashed to motor non-volatile flash (2026-06-18):
  - `G1` — drop / dispense (MR -50000, H, E) — carriage moves forward
  - `G2` — aspire / draw (MR +50000, H, E) — carriage moves backward
  - Step counts (±50000) confirmed by live manual jogging on this unit — do not change without re-testing
  - Labels use `G*` prefix — confirmed safe in LMDAE firmware (avoids reserved `F*`, `R*`)
- [x] Direction confirmed and corrected — initial drop/aspire assignment was reversed, fixed in script
- [x] Lead screw pitch unknown — all positions in raw steps only; do NOT assume real-world distance until pitch confirmed with Weifeng
- [x] Confirmed: **Python commands the motor** (not the robot directly), triggered by robot position signals per PRD architecture
  - Robot could technically do it via SPEL+ SockOpen/SockSend — but PRD chose Python for error handling (FR-PUMP-04/05) and logging
- [x] Architecture documented: `architecture/motor.md` (linear actuator section added)

### HMI Computer (172.16.9.10)
- [ ] RDP not yet set up — need to set password on PALD account OR disable blank password policy
- [ ] arena_api needs to be installed on HMI when deploying

---

## Software Architecture

### Architecture Docs (`architecture/`)
- [x] `architecture/config.md` — full config layer decisions documented
- [x] `architecture/data-layer.md` — full data layer decisions documented
- [x] `architecture/motor.md` — M-Drive rotary + linear actuator protocol and programs
- [x] `architecture/overhead_camera.md` — overhead camera hardware, driver connect flow, vision pipeline, calibration
- [x] `architecture/calibration.md` — homography calibration procedure and scripts
- [x] `architecture/ui.md` — UI architecture decisions
- [x] `architecture/vision.md` — **NEW (2026-06-24)** — full two-stage vision system:
  - Overhead detection (pipeline.py): algorithm, constants, calibration
  - EoA classification (eoa.py): full classifier, body centre computation, calibration
  - Orchestrator wiring: how both stages feed into the 9-step cycle
  - Software test suite: eoa_classifier.py, pipeline_demo.py
  - Threading model, pixel intensity conventions, what still needs doing

---

## Software Implementation

### Core (`cib/core/`)
- [x] `state_machine.py` — 7-state FSM (INITIALIZING, IDLE, RUNNING, CYCLE_STOPPING, E_STOP, PROTECTIVE_STOP, FAULT)
- [x] `events.py` — all typed event dataclasses
- [x] `orchestrator.py` — asyncio brain with 100ms safety/PLC polling and 9-step pick-and-place cycle
- Tests: 26/26 passing (`tests/core/test_state_machine.py`)

### Data Layer (`cib/data/`)
- [x] `models.py` — Run, Pick, SafetyEvent dataclasses + PickResult, RunStatus, ErrorCode, PickStatus constants
- [x] `database.py` — full SQLite layer:
  - create/finish/get run
  - start_pick (writes in_progress row immediately when cycle begins)
  - update_pick_status (called at every step — enables intelligent resume)
  - finish_pick, mark_pick_interrupted
  - get_resume_state (returns recovery_action + operator_message)
  - has_uncleared_safety_event (distinguishes e-stop from power cut)
  - record/clear safety events
- [x] `storage.py` — per-run CSV writer, appends immediately per pick, resume-safe
- Tests: 28/28 passing (`tests/data/`)

### Drivers (`cib/drivers/`)
- [x] All 6 mock drivers implemented (camera, lighting, PLC, pump, robot, safety)
- [ ] `cib/drivers/lighting/driver.py` — DCS-100E TCP SCPI driver — **CAN IMPLEMENT NOW** (all info known)
- [ ] `cib/drivers/camera/driver.py` — arena_api GigE driver — **CAN IMPLEMENT NOW** (SDK known, test_camera.py has working code)
- [ ] `cib/drivers/plc/driver.py` — pymodbus — partial, blocked on OQ-11 for light addresses but boilerplate writable
- [ ] `cib/drivers/safety/driver.py` — pymodbus — blocked on OQ-23 register map
- [ ] `cib/drivers/robot/driver.py` — TCP 172.16.9.15:5001 SPEL+ commands — partially writable, blocked on E-stop
- [ ] `cib/drivers/pump/driver.py` — blocked on OQ-14 (protocol unknown)
- Tests: 28/28 passing (`tests/drivers/test_mocks.py`)

### Config (`cib/config/`)
- [x] `loader.py` — loads machine config (YAML) and plate config (Excel); typed dataclasses; validates on load
- [x] `validator.py` — full validation for both config types with clear error messages
- [x] `config/machine_config.yaml` — template with all hardware IPs/ports/settings
- [x] `config/plate_config_template.xlsx` — Excel template for operators (with Instructions sheet)
- [x] `config/create_plate_template.py` — script to regenerate the Excel template
- Tests: 27/27 passing (`tests/config/`)

### Vision (`cib/vision/`)
- [x] `pipeline.py` — overhead detection (Method C): backlit panel locate → Hough dish detect → interior mask → Otsu inverse threshold → morph open → contour filter → centroid list. No minimum area filter — keeps tiny specimens. Calibration homography applied. Annotated frame saved to disk.
- [x] `calibration.py` — pixel → robot XY homography (CalibrationData, load_calibration, pixel_to_robot, pixel_list_to_robot). Now also has `EOA_CALIBRATION_PATH` constant for EoA camera calibration file.
- [x] `eoa.py` — **COMPLETE** (was stub, now full implementation). See `architecture/vision.md` for details.
  - Otsu inverse threshold (auto-adapts to any backlight level)
  - Coverage guard: >45% foreground = backlight off → no-go
  - Morphological close to bridge body/wing gaps
  - Center-first expanding-ring candidate search (EoA arm holds specimen near camera centre)
  - Skip HAIR/DEBRIS candidates; take first BUG (so a debris speck near centre doesn't block a real bug)
  - Body centre: intensity-weighted centroid of pixels < 70 gray (body only, excludes wings)
  - EoA calibration transform: body centre pixel → robot XY mm (`calibration_eoa.npy`, not yet run at lab)
  - If EoA calibration absent: refined_x/y = None → orchestrator falls back to overhead centroid (safe)
  - Saves annotated JPEG to `eoa_save_dir` if configured
  - 126/126 unit tests still passing after integration
- [x] `architecture/vision.md` — full two-stage vision architecture documented

### UI (`cib/ui/`)
- [ ] `app.py` — PyQt6 app entry — NOT YET STARTED
- [ ] `bridge.py` — thread-safe asyncio ↔ Qt signal bridge — NOT YET STARTED
- [ ] `main_window.py` — screen switcher — NOT YET STARTED
- [ ] `screens/pre_run.py` — field code, plate IDs, station map, config selection — NOT YET STARTED
- [ ] `screens/run_view.py` — live camera feed, counters, log panel, control buttons — NOT YET STARTED
- [ ] `screens/config_manager.py` — list + delete plate configs — NOT YET STARTED
- [ ] `screens/calibration.py` — engineer-only calibration — NOT YET STARTED
- [ ] `widgets/camera_feed.py` — live camera widget — NOT YET STARTED
- [ ] `widgets/safety_banner.py` — E-STOP / PROTECTIVE STOP overlays — NOT YET STARTED
- [ ] `widgets/station_map.py` — 4-station interactive map — NOT YET STARTED

### Camera Calibration (`camera_calibration/`)
All scripts are SEPARATE from the main cib/ software. Run manually at the lab, not during normal operation.

- [x] `calibration_target.pdf` — printable 7×6 checkerboard (20mm squares, 140×120mm).
      Print at "Actual Size" in Adobe Reader (confirmed prints at correct dimensions).
- [x] `step0_connect_cameras.py` — discovers all Lucid cameras, captures one test image from each
      (overhead + EoA), saves to output/overhead_test.png + eoa_test.png + camera_serials.json
- [x] `step1_detect_corners.py` — captures overhead image, finds 30 inner corners via
      findChessboardCorners + cornerSubPix, saves raw_capture.png / corners_detected.png /
      corners_numbered.png / corners.json
- [x] `step2_robot_grid_scan.py` — AUTOMATED calibration. User jogs EoA camera centre to C2
      (centre inner corner), robot auto-visits 4 surrounding corners (±40mm in X and Y),
      records robot XY at each, computes findHomography from 5 pixel↔robot pairs.
      RMSE achieved: 0.043mm (excellent).
      Saves H to output/ and C:\LabAutomation\config\calibration_overhead.npy
- [x] `step2_compute_homography.py` — MANUAL fallback. User types robot XY for 4 corners by hand.
- [x] `step3_verify_robot.py` — robot visits 15 reachable corners (col >= 3) using H to convert
      pixel->robot XY. Confirmed EoA crosshair lands accurately on each corner.
- [x] `step3_click_to_robot.py` — INTERACTIVE verification. Overhead snapshot shown, user clicks
      any point, robot EoA moves there via H. CONFIRMED WORKING end-to-end.
- [x] `eoa_live_feed.py` — live EoA camera feed with green crosshair at camera centre.
      Used during calibration jogging to visually align camera centre over corners.
- [x] `calibration_server.prg` — SPEL+ TCP server (port #201 = Python 2000) for calibration.
      Supports: STATUS, WHERE (returns current XY), MOVETO x,y (moves robot, returns actual XY), BYE.
      SPEL+ syntax: Go XY(rx, ry, CZ(Here), CU(Here)) — keeps Z and U unchanged.
      Load this in RC+ instead of robot_server.prg when running calibration.
- [x] `README.md` — run order, commands, when to recalibrate.
- [x] `output/calibration_overhead.npy` — THE H MATRIX. 3x3 float32 numpy array.
      Also copied to C:\LabAutomation\config\calibration_overhead.npy (runtime location).

## ⚠️  HOW TO CONVERT PIXEL → ROBOT XY (CRITICAL)

To convert any overhead camera pixel (px, py) to robot XY coordinates (mm):

```python
import numpy as np
import cv2

H = np.load(r"C:\LabAutomation\config\calibration_overhead.npy")
pt = np.array([[[px, py]]], dtype=np.float32)
result = cv2.perspectiveTransform(pt, H)
robot_x = float(result[0][0][0])
robot_y = float(result[0][0][1])
```

This is already implemented in `cib/vision/calibration.py`:
```python
from cib.vision.calibration import load_calibration
calib = load_calibration()          # loads H from C:\LabAutomation\config\
robot_x, robot_y = calib.pixel_to_robot(px, py)
```

**The H matrix was calibrated on 2026-06-17. RMSE = 0.043mm.**
**Recalibrate if: camera is moved, robot origin changes, dish fixture repositioned.**

### Main (`main.py`)
- [x] Entry point with USE_MOCKS toggle, build_drivers(), asyncio + UI runner

---

## Open Hardware Questions (blocking specific drivers)

| ID | Item | Blocking | Status |
|----|------|----------|--------|
| OQ-11 | PLC Modbus register map (status light, work light, event log addresses) | PLC driver light control | ⚠️ OPEN — ask David |
| OQ-14 | M-Drive protocol (TCP ASCII? Modbus? what commands?) | Pump driver | ⚠️ OPEN — ask David/Weifeng |
| OQ-23 | Sick Flexi Modbus register map (read: fault state/type; write: reset-ack) | Safety driver | ⚠️ OPEN — ask David |
| — | Robot E-stop circuit wiring | Robot driver physical testing | ⚠️ BLOCKER — tell David |
| — | Camera 2 (EoA) serial + IP identification | EoA camera driver | ⚠️ TODO — plug in 2nd camera |
| — | Camera IPs persistent (force_ip is temporary) | Camera driver on HMI | ⚠️ TODO — set via ArenaView |
| — | HMI RDP access | Remote development | ⚠️ TODO — set PALD password |
| — | RC90 remote I/O wiring to PLC | Robot restart via HMI | ⚠️ TODO — ask David |

---

## Next Up (priority order)

### Can do RIGHT NOW (no hardware needed, no open questions):
1. **UI (PyQt6)** — entire ui layer: app.py, bridge.py, main_window.py, all screens, all widgets
2. **`cib/drivers/lighting/driver.py`** — DCS-100E, all info known (TCP port 777, ASCII SCPI)
3. **`cib/drivers/camera/driver.py`** — arena_api, SDK installed, working test code exists in hardware_tests/
4. **Bug selection strategy** — implement corner-first traversal + visited-centroid tracking in orchestrator `_run_cycle()`
5. **Wire `eoa_save_dir` in `main.py`** — pass `eoa_save_dir=Path(config.paths.eoa_images)` to `Orchestrator(...)`

### Needs one lab session (not blocked on David):
6. **EoA calibration** (`calibration_eoa.npy`) — run checkerboard calibration with EoA camera. Same procedure as overhead. Script TBD. Until done, orchestrator falls back to overhead centroid (safe).

### Blocked on hardware/David:
7. **`cib/drivers/plc/driver.py`** — blocked on OQ-11 (register addresses)
8. **`cib/drivers/safety/driver.py`** — blocked on OQ-23 (Flexi register map)
9. **`cib/drivers/robot/driver.py`** — partially writable, blocked on E-stop wiring
10. **`cib/drivers/pump/driver.py`** — blocked on OQ-14 (M-Drive protocol)

---

## pymodbus 3.13 API Notes

- `count` must be a keyword argument: `read_holding_registers(address=0, count=10)`
- `slave` parameter not accepted on individual calls — set at client level or omit
- `write_coil(address=0, value=True)` — value is bool not int
- 1-based addressing in QModMaster = 0-based in pymodbus (subtract 1)
- **Must use endian-aware decoder** for Sick Flexi (known byte-order issue from prior impl)

## arena_api Notes

- `.whl` at: `C:\ProgramData\LUCID Vision Labs\ArenaView\ArenaPy\arena_api-2.9.5-py3-none-any.whl`
- Use direct Python path: `C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe`
  (NOT `conda run -n cib_env python` — env activation bug loses arena_api)
- `start_stream(2)` minimum (not 1 — OldestFirstOverwrite mode fails with 1)
- Nodemap access: `nodemap['NodeName']` (dict-style), NOT `nodemap.nodes['NodeName']`
- `force_camera_ip.py` must be run at start of each session (IP is temporary until ArenaView persistent IP is set)
