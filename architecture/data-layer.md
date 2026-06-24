# Data Layer — Architecture Decisions

## What It Does

Every time the robot picks a specimen and drops it in a well, the data layer
records it permanently. It also records the overall run, safety events, and
writes the CSV file the lab imports into their LIMS system.

Three files:
- `models.py` — defines what a run and a pick look like in Python
- `database.py` — saves everything to SQLite (`picks.db`)
- `storage.py` — writes the per-run CSV file row by row as picks complete

---

## Database Schema

### `runs` table — one row per time operator presses START

| Column | Type | Example | Notes |
|---|---|---|---|
| run_id | TEXT PRIMARY KEY | `run_20260616_143022` | Date + time, always unique |
| field_code | TEXT | `CIB-2026-001` | Set at pre-run setup |
| started_at | DATETIME | `2026-06-16 14:30:22` | When START was pressed |
| ended_at | DATETIME | `2026-06-16 15:45:10` | Null until run ends |
| status | TEXT | `in_progress` | `in_progress` / `completed` / `aborted` / `faulted` |
| total_picks | INTEGER | `48` | Updated at end of run |

**Critical:** Status is set to `in_progress` the moment START is pressed.
Only changed to `completed` when all wells are done. This is what enables
resume-after-crash.

### `picks` table — one row per specimen picked

| Column | Type | Example | Notes |
|---|---|---|---|
| pick_id | INTEGER PRIMARY KEY | auto | Auto-increment |
| run_id | TEXT | `run_20260616_143022` | Links to runs table |
| field_code | TEXT | `CIB-2026-001` | Duplicated here for easy CSV export |
| plate_id | TEXT | `PLATE-A` | Which plate station |
| well_position | TEXT | `A1` | Human-readable well label |
| result | TEXT | `success` | `success` / `fail` / `in_progress` |
| pick_status | TEXT | `BUG_IN_PIPETTE` | Fine-grained step within the pick cycle (see below) |
| timestamp_start | DATETIME | `2026-06-16 14:31:05` | When robot started moving to pick |
| timestamp_end | DATETIME | `2026-06-16 14:31:08` | When dispense completed. Null if interrupted |
| image_filename | TEXT | `eoa_001_a.tiff` | EoA image filename |
| robot_x | REAL | `234.5` | Robot pick coordinates |
| robot_y | REAL | `118.2` | Robot pick coordinates |
| error_code | TEXT | `NO_SPECIMEN` | Null on success |
| error_message | TEXT | null | Human-readable detail |

**Critical:** Every pick is written to DB **immediately when it completes**,
not at end of run. If power goes out, all completed picks are safe.

**Critical:** `pick_status` is updated at every step of the pick cycle —
not just at start and end. This is what enables intelligent resume after
an interruption. See "Pick Status Lifecycle" below.

### `safety_events` table — every e-stop or light curtain trip

| Column | Type | Example |
|---|---|---|
| event_id | INTEGER PRIMARY KEY | auto |
| run_id | TEXT | `run_20260616_143022` |
| event_type | TEXT | `estop` / `light_curtain` |
| triggered_at | DATETIME | timestamp |
| cleared_at | DATETIME | null until cleared |

---

## Pick Status Lifecycle

`pick_status` tracks exactly which step of the pick cycle the robot is on.
Updated in the DB at every transition. On resume after crash, reading this
column tells us precisely what the robot was doing.

```
IMAGING_OVERHEAD         ← taking overhead image from top camera
MOVING_EOA_CAMERA        ← EOA camera moving to position above specimen
IMAGING_EOA              ← taking zoomed EOA image (ring light ON)
COMPUTING_PICK_LOCATION  ← vision pipeline computing pick coordinates
MOVING_TO_PICK           ← robot arm moving to computed pick location
ASPIRATING               ← linear motor sucking specimen into pipette
BUG_IN_PIPETTE           ← specimen aspirated, robot moving toward well
DISPENSING               ← robot over well, linear motor dispensing
DONE                     ← dispense complete, result written
```

**The critical boundary is between `MOVING_TO_PICK` and `BUG_IN_PIPETTE`.**
Before that line: no specimen in pipette. After that line: specimen is in pipette.

**Why `ASPIRATING` is treated same as `BUG_IN_PIPETTE`:**
Aspiration takes ~0.5–1 second. If interrupted exactly during that window
we cannot know if it completed. Safer to assume specimen may be in pipette
and tell operator to check.

---

## Run ID Format

`run_YYYYMMDD_HHMMSS` — generated from system clock when START is pressed.

Example: `run_20260616_143022` = June 16 2026, 2:30:22 PM.

Always unique as long as operator doesn't press START twice in the same second
(enforced by disabling START button while run is in_progress).

---

## Querying Examples

```sql
-- All picks from a specific run
SELECT * FROM picks WHERE run_id = 'run_20260616_143022';

-- All picks for a field code across all runs
SELECT * FROM picks WHERE field_code = 'CIB-2026-001';

-- All failed picks ever
SELECT * FROM picks WHERE result = 'fail';

-- How many specimens were sorted today
SELECT COUNT(*) FROM picks
WHERE result = 'success'
AND date(timestamp_start) = '2026-06-16';

-- Find what a run was doing when it crashed
SELECT well_position, pick_status FROM picks
WHERE run_id = 'run_20260616_143022'
AND result = 'in_progress';
```

---

## Resume After Crash / Power Loss

### Scenario 1 — E-Stop or Light Curtain (software still running)
The run is still in memory. The state machine handles `RUNNING → E_STOP → RUNNING`.
When operator clears the fault and presses CONTINUE, picking resumes from the
next well. No data lost — every completed pick was already saved.

### Scenario 2 — Power cut or software crash (software dies completely)
On restart, the software checks the database for any run with `status = 'in_progress'`.

**If found:** Show operator:
> "Incomplete run found from [started_at]. Resume from well [next_well] or start fresh?"

- **Resume:** Load that run_id, query picks table for completed wells,
  read `pick_status` of the interrupted pick, determine recovery action (see below).
- **Start Fresh:** Mark old run as `status = 'aborted'`, create new run.

**Resume logic:**
```
All wells in plate config order: A1, A2, A3, A4, A5 ...
Already in picks table (result='success'): A1, A2, A3
Interrupted pick (result='in_progress'): A4
→ read pick_status of A4 to decide what to do with it
```

### Scenario 3 — Robot was mid-pick when power cut or safety triggered

This is the hardest case. The pick was `in_progress` in the picks table.
We now use `pick_status` to determine exactly what state the robot was in.

#### Recovery decision table

| pick_status at interruption | Specimen location | Recovery action |
|---|---|---|
| `IMAGING_OVERHEAD` | In sorting dish | Retry full pick cycle from overhead image |
| `MOVING_EOA_CAMERA` | In sorting dish | Retry full pick cycle |
| `IMAGING_EOA` | In sorting dish | Retry full pick cycle |
| `COMPUTING_PICK_LOCATION` | In sorting dish | Retry full pick cycle |
| `MOVING_TO_PICK` | In sorting dish | Retry full pick cycle |
| `ASPIRATING` | Unknown — may be in pipette | Treat same as BUG_IN_PIPETTE |
| `BUG_IN_PIPETTE` | In pipette | See below — depends on interruption type |
| `DISPENSING` | In pipette / partially in well | See below |

#### If `pick_status` is `BUG_IN_PIPETTE` or `ASPIRATING` or `DISPENSING`

**Pipette hardware note:** The pipette is driven by a linear motor (motorized
syringe). When stopped, the piston freezes in place — no passive force drops
the specimen. The specimen stays in the pipette as long as the motor holds position.

**Case A — E-stop or safety curtain (software + robot stopped, did NOT lose power)**
- Linear motor held position → specimen still in pipette
- SPEL+ program was aborted — must be restarted before robot can move
- After SPEL+ restarts and `Motor On` executes, robot knows its position
- Software sends `DISPENSE_AT {well_position}` — robot plans fresh path to well and dispenses
- **This recovery is possible.** Show operator:
  > "Specimen is in pipette. SPEL+ program must be restarted first.
  > Press DISPENSE to send robot to Well A4, or SKIP to discard this specimen."

**Case B — Power cut (robot lost all state, rehomed on restart)**
- Linear motor lost power during outage → piston position lost → specimen likely dropped
- Robot goes through full homing sequence on restart, which may further dislodge specimen
- Cannot recover automatically — specimen location unknown
- Show operator:
  > "Power was cut while specimen was in pipette. Specimen may be lost.
  > Check the pipette tip and Well A4 manually. Press CONTINUE or SKIP."

**How software distinguishes Case A vs Case B:**
On startup, if we find an `in_progress` run:
- If the safety event table has an uncleared event → e-stop/curtain → Case A
- If no uncleared safety event → power cut or crash → Case B

#### SPEL+ restart options (for Case A recovery)

When e-stop or safety curtain fires, the SPEL+ TCP server task is aborted.
Before the robot can move, it must be restarted. Options:

1. **HMI (preferred):** If RC90 remote I/O pins (`START`, `STOP`, `CONT`) are
   wired to PLC digital outputs, the HMI can trigger program restart:
   `HMI button → Python writes PLC coil → PLC pulses RC90 START pin → SPEL+ restarts`
   **Ask David if this wiring exists.**

2. **Teach pendant:** Operator selects program and presses Run on the pendant.
   Does not require the laptop. Standard RC90 feature.

3. **RC+ laptop (avoid):** Operator walks to laptop and presses F5.
   Not practical for production — operator should not need the laptop.

**`Motor On` requirement:**
After e-stop is cleared, servo power is cut. The SPEL+ program must execute
`Motor On` before any motion command. Our `robot_server.prg` startup sequence
must include this.

---

## CSV Output

Written to: `C:\LabAutomation\data\runs\{run_id}\{run_id}_picks.csv`

One row appended per completed pick (not buffered — written immediately).

Columns match the lab's LIMS import format (confirmed with Austin):
`field_code, plate_id, plate_position, well_position, image_filename,
timestamp_start, timestamp_end, result, error_code, error_message`

**Why append immediately and not write at end of run?**
If power goes out mid-run, the CSV has all completed picks. The lab can import
a partial run's data without waiting for a full re-run.

---

## File Locations

```
C:\LabAutomation\
  data\
    picks.db                          ← single SQLite file, entire history
    logs\
      app_2026-06-16.jsonl            ← structured daily log
    runs\
      run_20260616_143022\
        run_20260616_143022_picks.csv ← CSV for this run
        overhead\                     ← overhead TIFFs (debug, not delivered to lab)
        eoa\                          ← EoA TIFFs (primary lab deliverable)
```
