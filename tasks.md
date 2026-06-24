# Tasks To Do — CIB Automated Sorting Cell

One-off setup tasks, pending hardware actions, and deferred items.
Separate from the software backlog — these are things that need a person at the lab or a one-time config action.

---

## HARDWARE / SETUP TASKS

### [ONE-TIME] Set persistent static IP for both cameras in ArenaView GUI

**Why:** Both Lucid TRI245S-CC cameras boot with a link-local IP (`169.254.x.x`) because no static IP is stored in their flash. Every time the software starts, it has to `force_ip` the cameras to the correct subnet, which adds ~2 seconds of delay and an extra network operation. Once the static IP is written to the camera's NVM, it boots at the correct IP permanently and `force_ip` never runs.

**Do this once per camera. Takes ~2 minutes total.**

**Steps:**
1. Open **ArenaView** application (installed with Lucid SDK, should be on the HMI or laptop)
2. Connect to the camera network (172.16.9.x subnet)
3. In ArenaView, find the camera by serial number:

   | Camera | Serial | Target IP | Subnet | Gateway |
   |---|---|---|---|---|
   | Overhead | 253102168 | 172.16.9.21 | 255.255.255.0 | 172.16.9.1 |
   | EoA | 253102169 | 172.16.9.22 | 255.255.255.0 | 172.16.9.1 |

4. For each camera:
   - Select the camera in the device list
   - Go to **IP Configuration** (or **Network Configuration**)
   - Set **IP Configuration Mode** to **Static / PersistentIP**
   - Enter the IP address, subnet mask, and gateway from the table above
   - Click **Save** / **Apply** — this writes to the camera's non-volatile memory
   - Power cycle the camera (unplug PoE, wait 5s, reconnect)
   - Verify the camera reappears at the new IP in ArenaView

**After this is done:**
- Remove the `target_ip` fallback note from `architecture/overhead_camera.md` open items
- The driver's `force_ip` code path stays in the code as a safety net but will never execute

---

### [OPEN QUESTION — OQ-11] PLC Modbus register map for status light and work light

Ask David for:
- Holding register address for status light mode/colour/pattern
- Holding register address for work light on/off
- Any register for hardware event log readback

Currently the PLC driver is mocked. Config has placeholder register addresses.

---

### [OPEN QUESTION — OQ-23] Sick Flexi output register map

Ask David for the full output register bit map at address 256:
- Which bit is the P-Stop reset acknowledge write?
- Which bits correspond to which field devices (e-stop, light curtain, reset button)?

What we know so far (confirmed 2026-06-19 via rawwatch):
- `reg256 bit 15` = heartbeat (1Hz toggle)
- `reg256 bit 11` = system enabled (LOW = any fault active)
- `reg256 = 0x0917` (heartbeat masked) = safe/normal state
- `reg256 = 0x0113` = periodic automatic oscillation (bits 2 and 11 drop — likely a programmed output, not a physical trigger)
- Full bit map not yet confirmed by physical trigger tests

---

### [DEFERRED] Set up RDP remote access to HMI (DESKTOP-E74C49G)

David to configure RDP on the ICOP HMI at 172.16.9.10 so Tejas can access it remotely over Ewon VPN without being physically at the lab.

---

### [DEFERRED] Confirm lead screw pitch with Weifeng

For the linear actuator (Novanta LMDAE572C at 172.16.9.60): need to know steps-per-mm so the software can express pump volumes in µL rather than motor steps. Currently the driver uses raw step counts (G1 = -50000 steps, G2 = +50000 steps).

---

## SOFTWARE TASKS (deferred / not yet started)

These are tracked here until they move into active development.

- **EoA camera driver** — same `GigECameraDriver` class, serial `253102169`, target IP `172.16.9.22`, exposure 120000µs, pixel_format BGR8. Not yet wired into orchestrator.
- **EoA vision (`cib/vision/eoa.py`)** — go/no-go decision + refined centroid from EoA image. Not yet implemented.
- **Robot driver (`cib/drivers/robot/driver.py`)** — Epson T3-B TCP driver. E-stop fixed (2026-06-17). SPEL+ server program `robot_server.prg` deployed. Driver not yet implemented.
- **Safety driver (`cib/drivers/safety/driver.py`)** — Flexi Compact Modbus TCP reader at address 256. Bit map not fully confirmed (OQ-23). Mock in place.
- **PLC driver (`cib/drivers/plc/driver.py`)** — Logic PLC Modbus TCP for status/work lights. Register map not confirmed (OQ-11). Mock in place.
- **Full HMI UI** — replace `cib/ui/main_window.py` placeholder with full PyQt6 UI. Screens needed: pre-run setup, run view (camera feed + plate map), safety banner, config manager. See `architecture/overhead_camera.md` for full list.
- **Run management** — `_get_next_well_coords()` in orchestrator is a stub returning `(100, 100)`. Needs plate config + run state wired in.
- **CSV export** — data layer (`cib/data/`) is implemented but not called from orchestrator yet.
- **EoA image archival** — save TIFF per specimen with filename `{field_code}_{run_id}_{plate_id}_{well_position}_{timestamp}.tiff`. Austin requires these.
