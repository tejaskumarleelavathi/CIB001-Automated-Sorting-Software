"""
generate_status_checklist.py — build an Excel checklist mapping every PRD
requirement (Section 8 Functional Requirements + Section 9 NFRs) against
what is ACTUALLY implemented in the codebase today.

This is a point-in-time audit (2026-08-18). Re-run/update this script as
features get built so the checklist stays trustworthy — a stale checklist
is worse than no checklist.

Status values used:
  Done              — matches PRD, tested against real hardware
  Done (mocked)     — logic/code is complete and correct, but currently only
                       ever runs against a Mock driver (main.py has no switch
                       to turn on the real hardware for it yet)
  Done differently  — functionally works, but the PRD's described ownership/
                       architecture doesn't match what was actually built
                       (see Notes column)
  Partial           — some of the requirement is built, some isn't
  Built, not wired  — the code exists and looks correct, but nothing in the
                       running application ever calls it
  Not built         — no code for this yet (often a UI stub: "# TODO: implement ...")
  Blocked           — can't be finished without an answer from David (register
                       address, etc.) — see Notes
  Not verified       — plausibly works based on code structure, but was not
                       specifically checked/tested during this audit
  N/A               — no code required (process/scope note, not a feature)

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" software_tests/generate_status_checklist.py
"""

import os

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_PATH = os.path.join(os.path.dirname(__file__), "results", "PRD_Implementation_Checklist.xlsx")

# ── Data: (Section, ID, Requirement, Status, Notes) ─────────────────────────
# Notes is left "" (blank) whenever the implementation matches what the PRD
# describes — only filled in when it's different, missing, or blocked.

ROWS = [
    # ── 8.1 Pre-Run Setup ───────────────────────────────────────────────────
    ("8.1 Pre-Run Setup", "FR-SETUP-01", "Operator enters field code + Plate ID per station (4 fields) before run",
     "Not built", "Pre-run setup screen is a 1-line stub (cib/ui/screens/pre_run.py). Only a placeholder START/STOP window currently runs."),
    ("8.1 Pre-Run Setup", "FR-SETUP-02", "Operator selects a plate config file from a list populated from disk",
     "Not built", "list_plate_configs() already works in cib/config/loader.py — no UI calls it yet."),
    ("8.1 Pre-Run Setup", "FR-SETUP-03", "Operator activates plate stations via station map; requires Plate ID first",
     "Not built", "cib/ui/widgets/station_map.py is a 1-line stub."),
    ("8.1 Pre-Run Setup", "FR-SETUP-04", "Optional starting well per station, for mid-plate resume",
     "Not built", "No UI field exists, but PlateConfig.wells_from(start_well) is already implemented and ready in loader.py."),
    ("8.1 Pre-Run Setup", "FR-SETUP-05", "Config validated (schema + checksum) and locked read-only once a run starts",
     "Partial", "Schema validation exists (cib/config/validator.py). No checksum. No 'lock once running' mechanism."),
    ("8.1 Pre-Run Setup", "FR-SETUP-07", "Config file format = Excel (.xlsx); template shipped",
     "Done", ""),
    ("8.1 Pre-Run Setup", "FR-SETUP-08", "Config folder scanned dynamically, no restart needed",
     "Built, not wired", "list_plate_configs() would satisfy this — not called by any live UI yet."),
    ("8.1 Pre-Run Setup", "FR-SETUP-09", "File transfer onto the HMI disk is outside software scope",
     "N/A", "Process note, not a feature — nothing to build."),
    ("8.1 Pre-Run Setup", "FR-SETUP-10", "View/delete plate config files from an HMI screen",
     "Not built", "cib/ui/screens/config_manager.py is a 1-line stub."),

    # ── 8.2 Robot Control ───────────────────────────────────────────────────
    ("8.2 Robot", "FR-ROB-01", "Connect to robot on startup, verify before allowing a run",
     "Done", ""),
    ("8.2 Robot", "FR-ROB-05", "Robot connection loss triggers immediate cycle stop + operator alert",
     "Not verified", "ConnectionLostEvent -> FAULT transition exists in orchestrator; not specifically tested against a real dropped robot connection."),
    ("8.2 Robot", "FR-ROB-06", "Robot coordinates the syringe pump and pneumatic stage during pick-and-place",
     "Done differently", "Aspirate/dispense is driven directly by the robot's own SPEL+ program (sort_server.prg -> M-Drive over its own TCP link at port 503), not by cib/drivers/pump/driver.py. The Python pump driver only connects at startup and is otherwise unused."),

    # ── 8.3 Logic PLC ───────────────────────────────────────────────────────
    ("8.3 Logic PLC", "FR-PLC-01", "Connect to PLC on startup, poll at 100ms",
     "Done (mocked)", "_plc_poll_loop() runs every 100ms exactly as designed, but main.py has no ON-switch for the real PLC driver yet — it always uses the mock, even in today's real-hardware tests."),
    ("8.3 Logic PLC", "FR-PLC-02", "Control status light (green=running, amber=stopped, red=fault)",
     "Done (mocked)", "Register map confirmed 2026-06-18 and tested against real hardware separately — just not turned on inside the main app by default."),
    ("8.3 Logic PLC", "FR-PLC-03", "Read PLC hardware event log, store in structured log for traceability",
     "Blocked", "read_event_log() returns [] — register address not yet known (PRD OQ-11, needs David)."),
    ("8.3 Logic PLC", "FR-PLC-04", "PLC connection loss prevents a new run from starting",
     "Not verified", "Generic ConnectionLostEvent path exists; the specific 'block new run' gate was not confirmed."),
    ("8.3 Logic PLC", "(not in PRD)", "Work light on/off",
     "Built, not wired", "Coil address IS confirmed (coil 0 — hardware_tests/test_work_light.py). But cib/drivers/plc/driver.py's set_work_light() still says 'OQ-11 pending' and is a no-op — that comment is stale, this no longer needs David, just needs the address plugged in."),
    ("8.3 Logic PLC", "(not in PRD)", "Air pressure OK / Air pressure Low monitoring",
     "Discovered, unconfirmed", "hardware_tests/test_plc_air_pressure.py exists to find the exact coil for this signal, but per its own comment the address is still unconfirmed. Not a formal PRD requirement yet — worth deciding whether it should become one, since low air pressure could break the pipette."),

    # ── 8.4 Safety Circuit (Sick Flexi) ─────────────────────────────────────
    ("8.4 Safety", "FR-SAF-01", "Read safety status via ModbusTCP every 100ms",
     "Done (mocked)", "_safety_poll_loop() runs every 100ms; register bit map fully confirmed via rawwatch (2026-07-02). main.py has no ON-switch for the real safety driver yet — always mocked, even in today's real-hardware tests."),
    ("8.4 Safety", "FR-SAF-02", "Detect + log: light curtain trip, e-stop button, reset press, contactor change",
     "Done", "Fully decoded in FlexiCompactDriver.read_status() against the confirmed bit map."),
    ("8.4 Safety", "FR-SAF-03", "Fault detected -> transition to E_STOP/PROTECTIVE_STOP, block all motion",
     "Done", "_evaluate_safety() in the orchestrator."),
    ("8.4 Safety", "FR-SAF-04", "System shall NOT allow a run to start if any safety fault is active",
     "Done", "is_safe_to_start() only returns True in IDLE."),
    ("8.4 Safety", "FR-SAF-05", "Full e-stop hardware reset sequence required before CONTINUE enabled; also send light-curtain-latch reset-ack",
     "Blocked", "The reset-acknowledge Modbus write register is still unconfirmed (hardware_tests/test_flexi.py's own reset_pstop command says 'address TBC, ask David'). RESET_ACK_REG=None in the real driver, so this is a genuine no-op until David gives the register address — this is the real remaining piece of PRD OQ-23."),
    ("8.4 Safety", "FR-SAF-06", "Explicit operator START/CONTINUE required after reset — software never auto-resumes",
     "Done", "OperatorAcknowledgeEvent handling in orchestrator."),
    ("8.4 Safety", "FR-SAF-07", "Display the specific fault type (which e-stop, light curtain, contactor) on the UI",
     "Not built", "The data is available (SafetyStatusEvent carries estop_button_index etc.), but cib/ui/widgets/safety_banner.py is a 1-line stub — nothing displays it yet."),
    ("8.4 Safety", "FR-SAF-08", "Light curtain latching protective-stop: full detect -> banner -> clear -> CONTINUE -> resume flow",
     "Partial", "State machine + orchestrator flow is built correctly end-to-end in code; blocked from actually completing by the same reset-ack register gap as FR-SAF-05."),
    ("8.4 Safety", "FR-SAF-09", "Reset-ack sent only when light curtain is physically clear AND operator pressed CONTINUE",
     "Done (logic)", "Correctly gated in orchestrator (checks _light_curtain_tripped before calling send_reset_acknowledge). The underlying write itself is the no-op described in FR-SAF-05."),

    # ── 8.5 Syringe Pump (M-Drive) ───────────────────────────────────────────
    ("8.5 Pump", "FR-PUMP-01..06", "Software connects to M-Drive; commands aspirate/dispense; params configurable",
     "Done differently", "All of this works, but through the robot's own SPEL+ program (MotorAspire/MotorDrop in sort_server.prg) talking to the M-Drive directly over its own TCP socket at port 503 — not through cib/drivers/pump/driver.py as the PRD's architecture diagram implies. Functionally equivalent; different owner than documented."),

    # ── 8.6 Vision — Overhead Camera ─────────────────────────────────────────
    ("8.6 Vision (Overhead)", "FR-VIS-01..06", "Full pipeline: capture w/ backlight, detect all specimens, convert to robot coords, alert on zero detections, reuse latest image, debug-only saves",
     "Done", "cib/vision/pipeline.py — real, tested extensively against live hardware this session."),

    # ── 8.6a Specimen Tracking ───────────────────────────────────────────────
    ("8.6a Specimen Tracking", "v1 visited set", "Mark visited, PRD text says 1.5mm tolerance radius",
     "Done differently", "Implemented as an EXACT match on a 0.1mm snap grid, no radius at all — a deliberate upgrade decided after calibration RMSE (0.043mm) showed any real radius risked skipping adjacent bugs less than 1mm apart."),
    ("8.6a Specimen Tracking", "v2 state machine", "Per-centroid VISITED/PICKED/DEBRIS/RETRY/FAILED state machine",
     "Not built", "PRD explicitly says 'Do not implement in v1' — correctly not built yet."),

    # ── 8.7 Vision — EoA Camera ──────────────────────────────────────────────
    ("8.7 Vision (EoA)", "FR-EOA-01", "Backlight + ring light images before pick; ring light image is primary archival deliverable",
     "Done", ""),
    ("8.7 Vision (EoA)", "FR-EOA-02", "Analyze EoA image(s): go/no-go decision + refined pick coordinates",
     "Done", ""),
    ("8.7 Vision (EoA)", "FR-EOA-03", "If no pick, move clear and return to step 1; configurable alternate-coordinate retries",
     "Done", ""),
    ("8.7 Vision (EoA)", "FR-EOA-04", "Post-pick confirmation via overhead re-capture (not a dedicated EoA image)",
     "Not verified", "Did not specifically confirm this exact step is wired into _run_cycle() — worth double-checking against the code directly."),
    ("8.7 Vision (EoA)", "FR-EOA-05", "Log pick result with timestamps, coordinates, pass/fail status",
     "Built, not wired", "This is exactly what cib/data/database.py + storage.py are designed for. Fully built (including crash-resume logic), never called from the pick cycle."),
    ("8.7 Vision (EoA)", "FR-EOA-06", "Configurable number of consecutive pick failures triggers a cycle stop",
     "Not verified", "No failure-streak counter/threshold was found in the orchestrator during this audit — worth confirming."),
    ("8.7 Vision (EoA)", "FR-EOA-07", "Save all EoA images as TIFF (PNG fallback); filenames in separate CSV columns per lighting variant",
     "Partial", "Images ARE saved to disk correctly (cv2.imwrite, backlight_on/backlight_off folders). The CSV column linkage (image_filename_backlight / image_filename_ringlight) doesn't exist anywhere because storage.py is never called."),

    # ── 8.8 Lighting Controller ──────────────────────────────────────────────
    ("8.8 Lighting", "FR-LIT-01..05", "Backlight on/off around capture; configurable timing; connection-loss handling",
     "Done", "Real DCS-100E driver, tested extensively against live hardware this session."),

    # ── 8.9 Operator UI ──────────────────────────────────────────────────────
    ("8.9 Operator UI", "FR-UI-01", "Pre-run setup screen: field code, 4x Plate ID, config dropdown, station map, starting well, gated START",
     "Not built", "cib/ui/screens/pre_run.py is a 1-line stub."),
    ("8.9 Operator UI", "FR-UI-02", "Plate config selection list populated dynamically, no restart needed",
     "Not built", "Loader helper exists; no UI to call it."),
    ("8.9 Operator UI", "FR-UI-03/04", "START / ABORT / CYCLE STOP / CONTINUE run-control buttons",
     "Partial", "Current placeholder window (cib/ui/main_window.py) only has START / STOP / SHUTDOWN. No ABORT button. No CYCLE-STOP-vs-hard-STOP distinction surfaced. No CONTINUE-labeled state."),
    ("8.9 Operator UI", "FR-UI-05", "Light curtain protective-stop banner with light-curtain-gated CONTINUE button",
     "Not built", "cib/ui/widgets/safety_banner.py is a 1-line stub. The underlying gating logic (_light_curtain_tripped check) is already correct in the orchestrator — nothing displays it."),
    ("8.9 Operator UI", "FR-UI-06", "E-stop alert showing the specific e-stop source",
     "Not built", "Same as above — data exists in SafetyStatusEvent, no display."),
    ("8.9 Operator UI", "FR-UI-07..10", "Live system state, live camera feed, pick/pass/fail counters, elapsed time, scrolling log panel",
     "Partial", "Only a plain state-name badge exists. No camera feed widget (cib/ui/widgets/camera_feed.py is a stub), no counters, no log panel."),
    ("8.9 Operator UI", "FR-UI-11", "Two roles: Operator and Engineer, with different screen access",
     "Not built", "No authentication or role concept anywhere in the current placeholder UI."),
    ("8.9 Operator UI", "FR-UI-12", "Config management screen (list + delete), both roles",
     "Not built", "cib/ui/screens/config_manager.py is a 1-line stub."),
    ("8.9 Operator UI", "FR-UI-13", "Calibration screen (Engineer-only)",
     "Not built", "cib/ui/screens/calibration.py is a 1-line stub. Calibration itself works fine, but only via standalone scripts in camera_calibration/, not an in-app screen."),
    ("8.9 Operator UI", "FR-UI-15", "Manual Plate ID text entry (barcode scanning deferred to a future version)",
     "Not built", "Tied to the FR-UI-01 pre-run screen, which doesn't exist yet."),

    # ── 8.10 Run Management ──────────────────────────────────────────────────
    ("8.10 Run Management", "FR-RUN-01", "Each run has a unique timestamp-based ID, used as the key for all logged data",
     "Built, not wired", "Run dataclass + Database.create_run() already implement this correctly; never called."),
    ("8.10 Run Management", "FR-RUN-02", "Field code + Plate ID required for every activated station before a run starts",
     "Not built", "Tied to the pre-run UI, which doesn't exist yet."),
    ("8.10 Run Management", "FR-RUN-03", "A plate config must be loaded before starting a run",
     "Not built", "Tied to the pre-run UI."),
    ("8.10 Run Management", "FR-RUN-04", "Config files are locked read-only once a run starts",
     "Not built", ""),
    ("8.10 Run Management", "FR-RUN-05", "Abort clears all in-progress run state/data, returns to IDLE",
     "Done", "OperatorAbortEvent handling in orchestrator."),
    ("8.10 Run Management", "FR-RUN-06", "Mid-plate field code change = CYCLE STOP -> ABORT -> new run with new field code + starting well",
     "Not built", "Depends on the pre-run UI and FR-SETUP-04, neither of which exist yet."),
    ("8.10 Run Management", "FR-RUN-07", "Well fill sequence tracked per plate station; resumes from operator-specified starting well",
     "Done differently", "Currently ONE single global well-index counter for a single test-scoped station/config (main.py's TEST_PLATE_CONFIG) — not per-station, and not resumable via any operator input yet. This is the 4-station work already in progress."),

    # ── Section 9 — Non-Functional Requirements ──────────────────────────────
    ("9. NFR", "NFR-01", "Software e-stop response time < 200ms (secondary layer; hardware circuit is the primary <10ms stop)",
     "Not measured", "Poll loop runs at 100ms so it's architecturally capable, but never benchmarked — and currently only ever exercised against mocked safety hardware."),
    ("9. NFR", "NFR-02", "System runs continuously for 8-hour shifts without requiring a restart",
     "Not tested", ""),
    ("9. NFR", "NFR-09", "Every operator action + system event is timestamped and written to structured log",
     "Done", "structlog used throughout; confirmed via today's real-hardware test logs."),
    ("9. NFR", "NFR-10", "After an e-stop, operator can resume (re-home + re-detect) without restarting software",
     "Done", "_safe_recovery() implements exactly this — though see the cross-cutting 'specimen in pipette' gap below, which predates real pick-place being wired in."),
    ("9. NFR", "NFR-12", "CSV export written incrementally per pick (not batched at run end)",
     "Built, not wired", "storage.py already does exactly this correctly (flush to disk on every append_pick call) — just never called."),

    # ── Cross-cutting gaps worth flagging on their own ──────────────────────
    ("Cross-cutting", "-", "Data layer: SQLite (runs/picks/safety_events tables) + per-run CSV writer, incl. full crash/e-stop resume decision logic (RETRY / DISPENSE_AT_WELL / OPERATOR_CHECK)",
     "Built, 0% wired", "cib/data/database.py + cib/data/storage.py are genuinely excellent and match the PRD's intent closely (including the specimen-in-pipette recovery cases) — but are only ever called from their own unit tests, never from the running orchestrator. This is the single highest-value gap to close."),
    ("Cross-cutting", "-", "Safety recovery does not yet know pick-place is real",
     "Gap", "orchestrator._safe_recovery()'s own docstring says 'pick-place is a TODO stub, so the robot is never in a pick state' — that's now false, real aspirate/dispense was wired in today. If e-stop hits while a specimen is in the pipette tip, recovery still just unconditionally homes. The DB layer already designed the right decision tree for this (get_resume_state() -> DISPENSE_AT_WELL) — it just isn't called yet."),
    ("Cross-cutting", "-", "cib/ui/bridge.py — intended UI <-> orchestrator glue layer",
     "Not built", "1-line stub. The current placeholder main_window.py calls orchestrator methods directly instead, bypassing the architecture this file was meant to provide."),
    ("Cross-cutting", "-", "main.py has no ON-switch for real Safety or PLC drivers",
     "Gap", "USE_REAL_ROBOT / USE_REAL_CAMERA_OVERHEAD / USE_REAL_CAMERA_EOA / USE_REAL_LIGHTING flags exist; there is no USE_REAL_SAFETY or USE_REAL_PLC. Both are unconditionally mocked in build_drivers() regardless of any flag, even during today's real-hardware pick/drop testing."),
]

STATUS_COLOURS = {
    "Done":                 "C6EFCE",  # green
    "Done (mocked)":        "FFEB9C",  # yellow
    "Done (logic)":         "FFEB9C",
    "Done differently":     "FFEB9C",
    "Partial":              "FFEB9C",
    "Built, not wired":     "FCE4D6",  # orange-ish
    "Not built":            "FFC7CE",  # red
    "Not verified":         "D9D9D9",  # grey
    "Not tested":           "D9D9D9",
    "Not measured":         "D9D9D9",
    "Blocked":              "FFC7CE",
    "Discovered, unconfirmed": "D9D9D9",
    "Built, 0% wired":      "FCE4D6",
    "Gap":                  "FFC7CE",
    "N/A":                  "F2F2F2",
}


def build() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PRD Checklist"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    thin = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top", horizontal="left")

    headers = ["Section", "ID", "Requirement (from PRD)", "Status", "Notes (only if different from PRD / gap)"]
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    row_idx = 2
    for section, fid, requirement, status, notes in ROWS:
        values = [section, fid, requirement, status, notes]
        for col, val in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.border = border
            cell.alignment = wrap
            if col == 4:
                colour = STATUS_COLOURS.get(status, "FFFFFF")
                cell.fill = PatternFill("solid", fgColor=colour)
                cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
        row_idx += 1

    col_widths = [20, 16, 55, 18, 70]
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:E{row_idx - 1}"

    wb.save(OUT_PATH)
    print(f"Saved: {OUT_PATH}  ({row_idx - 2} rows)")


if __name__ == "__main__":
    build()
