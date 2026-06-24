# Config Layer — Architecture Decisions

## Two Config Files, Two Audiences

| File | Audience | Format | Changes when? |
|---|---|---|---|
| Plate config | Lab operators | Excel (.xlsx) | New plate type, new well layout |
| Machine config | Engineers only | YAML | Hardware changes, IP changes, tuning |

Operators never touch the machine config. Engineers never need to touch the plate config during normal operation.

---

## 1. Plate Config (Excel)

### Location
`C:\LabAutomation\config\plate_configs\`

One file per plate type. Operator selects from a dropdown on the HMI at pre-run setup. Multiple files can exist simultaneously (e.g. `96well_standard.xlsx`, `48well_deep.xlsx`). No software restart required when new files are added.

### Format — Excel (.xlsx)

Excel chosen over CSV because:
- Operators are comfortable in Excel
- No "save as CSV" export step — they just save the file
- Excel can include a template with column headers pre-filled and instructions in a second sheet

**Required columns (exact header names, case-insensitive):**

| Column | Example | Notes |
|---|---|---|
| `well` | `A1` | Human-readable well label. Austin requires this in CSV output. |
| `x` | `234.5` | Robot X coordinate in mm (robot coordinate system) |
| `y` | `118.2` | Robot Y coordinate in mm (robot coordinate system) |

**Optional columns (can be added later):**

| Column | Example | Notes |
|---|---|---|
| `aspirate_volume` | `5.0` | µL — overrides machine config default if present |
| `aspirate_speed` | `2.0` | µL/s — overrides machine config default if present |
| `dispense_volume` | `5.0` | µL |
| `dispense_speed` | `2.0` | µL/s |

### Load Order = Row Order

**The order of rows in the Excel file defines the order the robot fills wells.**

Example: if the file has rows `A1, B2, A2` in that order, the robot fills A1 first, then B2, then A2.

This means the lab can define any fill order they want just by arranging rows in Excel — no separate load order file, no extra column needed. The operator specifies a starting well at pre-run setup (FR-SETUP-04); the software finds that well's row index in the file and starts from there.

### How to Measure Robot X and Y Coordinates

Robot XY coordinates are in the **Epson T3-B robot's world coordinate system** — measured in millimetres from the robot's origin point (defined when the robot was installed).

**To find the XY coordinate of a well:**

1. Open RC+ software on the laptop
2. Use the teach pendant or the RC+ jog panel to manually move the robot arm
3. Jog the robot until the pipette tip is positioned directly over the centre of the target well
4. Read back the current X and Y values from the RC+ position display
5. Write those values into the plate config Excel file for that well row

You do this once per well, for every well in the plate. For a 96-well plate that is 96 measurements. In practice:

- Measure the four corner wells (A1, A12, H1, H12)
- Measure one or two middle wells to verify spacing is uniform
- If spacing is perfectly uniform (standard plates are), you can calculate the remaining well positions mathematically and only physically verify a sample

**This is what "calibration" means in this context** — it is not a software calibration routine. It is a one-time physical measurement process done by an engineer on first installation, and again if the plate fixture is moved or replaced. Results go directly into the Excel plate config file.

Z coordinate (height) is handled separately inside the SPEL+ robot program — the robot knows how deep to lower the pipette. Z is not in the plate config.

### Template File
A template Excel file (`plate_config_template.xlsx`) shall be shipped with the system. It contains:
- Sheet 1: the data table with `well`, `x`, `y` column headers pre-filled, and example rows
- Sheet 2: instructions for operators explaining what each column means and how to measure coordinates

---

## 2. Machine Config (YAML)

### Format — YAML

YAML chosen as it is the industry standard for Python automation applications (used in ROS, Ansible, Docker Compose, most Python lab automation frameworks). It is human-readable, supports comments, and handles nested structure cleanly.

### Location
`C:\LabAutomation\config\machine_config.yaml`

Single file. One copy on the machine. Engineers edit it directly when hardware changes (IP address moved, camera swapped, timing needs tuning). Never touched during normal operation.

### Full Structure

```yaml
# CIB Automated Sorting Cell — Machine Configuration
# Edit this file when hardware changes. Do not modify during a run.

robot:
  ip: "172.16.9.15"
  port: 2000
  connect_timeout_s: 5.0
  command_timeout_s: 10.0

plc:
  ip: "172.16.9.11"
  port: 502
  poll_interval_ms: 100
  unit_id: 1
  # Modbus register addresses (from Modbus Mapping.xlsx)
  registers:
    status_light_mode: 0        # 40001
    status_light_pattern: 1     # 40002
    status_light_direction: 2   # 40003
    status_light_speed: 3       # 40004
    status_light_primary_color: 5   # 40006
    status_light_primary_intensity: 6
    status_light_secondary_color: 7
    status_light_secondary_intensity: 8
  coils:
    status_light_demo: 0        # coil 00001

flexi:
  ip: "172.16.9.12"
  port: 502
  poll_interval_ms: 100
  unit_id: 1
  # Input register addresses (from Sick Flexi — confirmed via flexi_watch_raw.py)
  registers:
    status_base: 2000           # input registers 2000-2007

overhead_camera:
  ip: "172.16.9.21"
  serial: ""                    # fill in after confirmed
  exposure_us: 10000
  gain_db: 0.0

eoa_camera:
  ip: "172.16.9.22"             # force_ip from 172.16.9.41 on first connect
  ip_original: "172.16.9.41"    # camera's factory IP (used for force_ip)
  serial: ""                    # fill in after confirmed
  exposure_us: 5000
  gain_db: 0.0

backlight:
  ip: "172.16.9.30"             # TODO: confirm IP
  port: 6006                    # TODO: confirm port
  current_ma: 250
  connect_timeout_s: 2.0

ring_light:
  # Controlled via robot digital output — no separate network device
  robot_output_pin: 12          # Out 12 on RC90

pipette:
  # M-Drive syringe pump — protocol TBD (OQ-14)
  # Placeholder values — update when David/Weifeng confirm interface
  interface: "tcp"              # "tcp" or "serial"
  ip: ""                        # fill in when known
  port: 503
  aspirate_volume_ul: 5.0       # default, overridable per well in plate config
  aspirate_speed_ul_per_s: 2.0
  dispense_volume_ul: 5.0
  dispense_speed_ul_per_s: 2.0

paths:
  db: 'C:\LabAutomation\data\picks.db'
  plate_configs: 'C:\LabAutomation\config\plate_configs'
  runs_output: 'C:\LabAutomation\data\runs'
  logs: 'C:\LabAutomation\data\logs'

vision:
  # Overhead camera detection
  detection_min_area_px: 50
  detection_max_area_px: 5000
  backlight_lead_time_ms: 50    # how long before capture to turn backlight on

logging:
  level: "INFO"                 # DEBUG / INFO / WARNING
  retention_days: 30
```

### What Lives Here vs. Plate Config

If it changes per plate type → plate config Excel.
If it changes when hardware is swapped or network changes → machine config YAML.
If it is set by the operator per run → entered on the HMI pre-run setup screen (field code, plate IDs, starting well) and stored in the `runs` DB table.

---

## 3. Validation Rules

### Plate config validation (on load)
- Required columns present: `well`, `x`, `y`
- No duplicate well labels
- No blank cells in required columns
- X and Y are numeric
- At least 1 row of data

### Machine config validation (on startup)
- All required keys present (robot, plc, flexi, cameras, paths)
- IPs are valid IPv4 format
- Numeric values are in sensible ranges (ports 1-65535, timeouts > 0)
- All paths exist on disk (create if missing for data output dirs)

If machine config fails validation → software refuses to start, shows error.
If plate config fails validation → operator sees error on HMI, cannot start run.

---

## 4. Config Access Pattern

The loaded config is a single Python object passed into every driver and the orchestrator at startup. Drivers do not read files — they receive config values through their constructor.

```python
# At startup (main.py)
machine_cfg = load_machine_config()     # reads YAML once
plate_cfg   = load_plate_config(path)  # reads Excel when operator selects

robot_driver = RobotDriver(ip=machine_cfg.robot.ip, port=machine_cfg.robot.port)
```

Config is read-once at startup / run-start and then locked. No re-reading mid-run.
