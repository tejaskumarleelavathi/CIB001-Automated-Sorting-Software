# Overhead Camera — Architecture & Implementation Notes

**Date written:** 2026-06-19  
**Status:** Phase 1 complete — capture + save working. Full UI integration pending.

---

## What the overhead camera does (PRD reference)

- Images the sorting dish to detect specimen locations (insect centroids)
- Output: pixel centroids → converted to robot XY via calibration homography
- Images are **debug-only** — Austin (the lab) does not require overhead images as a deliverable
- Austin's deliverables are: EoA TIFF archival images, field code, plate ID, well position in CSV

---

## Hardware

| Property | Value |
|---|---|
| Model | Lucid TRI245S-CC |
| Serial number | 253102168 |
| IP (force-assigned) | 172.16.9.21 |
| SDK | arena_api (Python wheel, installed in cib_env) |
| PoE switch | Stride PoE switch — camera powered + data over one cable |
| Pixel format | Mono8 (grayscale — sufficient for blob detection) |
| Exposure | 2000µs (backlight on). Auto if backlight off. |
| Gain | 0 dB |

Camera is identified by serial number, not IP. IP is force-assigned at connect time using `force_camera_ip.py`. IP resets on camera power cycle.

---

## Backlight (DCS-100E)

| Property | Value |
|---|---|
| Model | Advanced Illumination DCS-100E |
| IP | 172.16.9.20 |
| Port | 777 |
| Protocol | ASCII SCPI over TCP |
| Current | 250 mA |

**Behaviour:**  
Backlight turns ON immediately after all devices connect (in `Orchestrator._startup()`).  
Stays ON for the entire session. No per-cycle toggling.

Commands (confirmed from hardware_tests/test_lighting.py):
```
SET:LEVEL:CHANNEL1,250;SET:LEVEL:CHANNEL2,250;SET:LEVEL:CHANNEL3,250;  → set 250mA on all 3 channels
SET:MODE:CHANNEL1,1;SET:MODE:CHANNEL2,1;SET:MODE:CHANNEL3,1;           → turn all channels ON
SET:MODE:CHANNEL1,0;SET:MODE:CHANNEL2,0;SET:MODE:CHANNEL3,0;           → turn all channels OFF
DISCONNECT;                                                              → clean socket close
```
Commands are semicolon-terminated ASCII, batched into one TCP send per operation.
⚠️ NOT standard SCPI (`OUTP ON` / `SOUR:CURR`) — those commands don't work on this device.

---

## Driver: `cib/drivers/camera/driver.py`

**Class:** `GigECameraDriver`

### Why force_ip exists — and when it goes away

GigE Vision has two separate communication layers:

| Layer | Protocol | Used for | Subnet required? |
|---|---|---|---|
| Discovery | GVC broadcast (MAC-level) | Finding cameras by serial | No — works cross-subnet |
| Streaming | GVSP (UDP/IP unicast) | Sending pixel data to host | **Yes — same subnet required** |

The camera boots with a link-local IP (`169.254.x.x`) when no static IP is stored in its flash. The host is on `172.16.9.x`. Discovery works (camera IS found by serial), but `create_device()` → GVSP streaming fails because the subnets differ and IP routing between them doesn't exist.

There is no way around this at the protocol or library level. `force_ip()` is the GigE Vision standard solution — it sends a MAC-broadcast command (not IP-routed) that tells the camera to reprogram its IP to `172.16.9.21` for this session.

**Permanent fix (one-time):** Set a persistent static IP in the camera's NVM via ArenaView GUI. After that the camera boots at `172.16.9.21` every time and `force_ip` never runs. See `tasks.md`.

### Connect flow

1. Wait up to 10s for cameras to appear on `system.device_infos`
2. Filter by serial number → find `253102168`
3. Try `system.create_device(device_infos=target_info)` directly
   - **If camera already has correct IP (after ArenaView NVM setup):** succeeds immediately
   - **If camera is on wrong subnet (link-local):** catches `GC_ERR_INVALID_ADDRESS`, falls back to:
     - `system.force_ip(serial, ip_int, subnet_int, gateway_int)` — Layer-2 MAC broadcast
     - Wait 1.5s for camera network stack to restart
     - Rediscover by serial → `create_device()` again
4. Configure: `PixelFormat=Mono8`, `ExposureAuto=Off`, `ExposureTime=2000`, `GainAuto=Off`
5. `device.start_stream(2)` — 2 buffers minimum

**Capture flow (runs in thread pool via `run_in_executor`):**
1. `device.get_buffer(timeout=5000)`
2. Read bytes: `(ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))`  
   ⚠️ Must use `buf.pbytes` + `ctypes.addressof()` — NOT `buf.pdata` (causes integer error)
3. `np.ctypeslib.as_array(raw).copy()` → `.reshape(height, width)`
4. `device.requeue_buffer(buf)`
5. Return numpy array

First capture drains 3 warmup frames so exposure settles.

---

## Vision pipeline: `cib/vision/pipeline.py`

**Function:** `run_overhead_detection(frame, save_dir, save_format)`

**Flow:**
1. Resize frame to 25% scale (faster processing)
2. Gaussian blur (5×5)
3. Binary threshold (value=15)
4. Morphological open + close (ellipse 3×3 kernel)
5. Find contours → filter by area (50–2000 px)
6. Compute centroids, scale back to original resolution
7. Convert pixel centroids → robot XY via calibration homography (`load_calibration()`)
   - Falls back to raw pixel coords if calibration file not present (dev mode)
8. Save annotated image to `save_dir` if configured

**Image saving:**
- Filename: `overhead_{YYYYMMDD}_{HHMMSS}_{ms}.png`
- Annotated with green circles at detected centroid positions + specimen count label
- Format: PNG (configurable to TIFF via `vision.overhead_save_format` in machine_config.yaml)

---

## Orchestrator integration: `cib/core/orchestrator.py`

**Startup:** After all devices connect → `lighting.set_backlight(True)` → emit `AllDevicesReadyEvent`

**Per-cycle capture (Step 1):**
```python
frame = await d.camera_overhead.capture()   # async, runs in thread pool
overhead_result = await loop.run_in_executor(None, self._run_vision_overhead, frame)
```

**Vision call passes save config:**
```python
def _run_vision_overhead(self, frame):
    from cib.vision.pipeline import run_overhead_detection
    return run_overhead_detection(
        frame,
        save_dir=self._overhead_save_dir,
        save_format=self._overhead_save_format,
    )
```

**Orchestrator constructor parameters added:**
- `overhead_save_dir: Path | None` — where to save images (None = don't save)
- `overhead_save_format: str` — "png" or "tiff"
- `overhead_save_images: bool` — master on/off switch

---

## Config: `config/machine_config.yaml`

```yaml
overhead_camera:
  serial: "253102168"
  ip: "172.16.9.21"      # force_camera_ip.py assigns this at session start
  exposure_us: 2000
  gain_db: 0.0
  pixel_format: "Mono8"

backlight:
  ip: "172.16.9.20"
  port: 777
  current_ma: 250
  connect_timeout_s: 2.0

paths:
  overhead_images: 'C:\LabAutomation\data\overhead_images'   # prod path

vision:
  overhead_save_images: true
  overhead_save_format: 'png'
```

In dev mode (`USE_MOCKS=True`), `main.py` overrides `overhead_save_dir` to `output/overhead_images/` in the project root.

---

## Threading model: `main.py` + `cib/ui/app.py`

```
Main thread:    PyQt6 event loop (QApplication.exec())
                └── MainWindow polls orchestrator.state every 500ms (QTimer)
                └── Button clicks call orchestrator.operator_start/stop() — thread-safe

Daemon thread:  asyncio event loop
                └── orchestrator.run()
                     ├── _startup() — connects all devices, turns on backlight
                     ├── _safety_poll_loop() — polls Flexi every 100ms
                     ├── _plc_poll_loop() — polls PLC every 100ms
                     └── _event_loop() → _run_cycle() per cycle
```

**Why asyncio in background thread:**  
PyQt6 requires the main thread for its event loop. asyncio can run in any thread.  
`operator_start/stop()` use `asyncio.Queue.put_nowait()` which is thread-safe.

---

## Current UI: `cib/ui/main_window.py` (Phase 1 placeholder)

**This is a minimal placeholder**, NOT the final HMI UI.

What it has:
- State badge (INITIALIZING / IDLE / RUNNING / STOPPING / E-STOP / FAULT)
- START button — enabled only in IDLE state
- STOP button — enabled only in RUNNING state
- Capture counter (counts files in `output/overhead_images/`)

**What it does NOT have (to be built in Phase 2 UI):**
- Pre-run setup screen: field code entry, plate ID, starting well selection, plate config file picker
- Live overhead camera feed widget showing annotated image in real-time
- Plate station map showing 4 plate stations with per-well fill status
- Safety banner: e-stop type, light curtain status, CONTINUE button for protective stop reset
- EoA camera feed widget
- Per-run CSV export status
- Work light / status light control UI

**When building the full UI**, replace `cib/ui/main_window.py` with the full HMI screens. The orchestrator API doesn't change — only the UI file changes. All orchestrator methods remain the same:
- `orchestrator.operator_start()` — start a run
- `orchestrator.operator_stop()` — graceful cycle stop
- `orchestrator.operator_abort()` — immediate abort, robot parks
- `orchestrator.operator_acknowledge()` — resume after protective stop
- `orchestrator.state` — current state (read any thread)

---

## Image naming decisions (confirmed 2026-06-19)

| Image type | Filename format | Format | Saved where | Required by Austin? |
|---|---|---|---|---|
| Overhead (debug) | `overhead_{YYYYMMDD}_{HHMMSS}_{ms}.png` | PNG | `output/overhead_images/` (dev) or `C:\LabAutomation\data\overhead_images\` (prod) | No |
| EoA (archival) | `{field_code}_{run_id}_{plate_id}_{well_position}_{timestamp}.tiff` | TIFF | `C:\LabAutomation\data\runs\{run_id}\` | Yes — referenced in CSV for LIMS |

---

## What to do before full production use

1. Run `force_camera_ip.py` at the start of each session (camera IPs reset on reboot)
2. Confirm `overhead_camera.serial = "253102168"` in machine_config.yaml is correct
3. Confirm backlight IP `172.16.9.20:777` is reachable
4. Wire calibration: `load_calibration()` loads `C:\LabAutomation\config\calibration_overhead.npy` — recalibrate if camera or dish fixture moved

---

## Open items

- **[PENDING — see tasks.md]** Camera IPs reset on reboot — set persistent static IP in ArenaView GUI for both cameras. One-time action. After this, `force_ip` fallback never runs and connect is instant.
- EoA camera driver: same `GigECameraDriver` class, serial `253102169`, target_ip `172.16.9.22`, exposure 120000µs, pixel_format BGR8
- EoA vision (`cib/vision/eoa.py`): not yet implemented
