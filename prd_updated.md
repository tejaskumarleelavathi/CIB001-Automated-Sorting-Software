# Product Requirements Document

## Lab Automation — Automated Sample Sorting Cell

**Version:** 0.6
**Status:** Updated — David (DTM) review comments incorporated
**Last Updated:** 2026-06-08
**Author:** Tejas Kumar Leelavathi

---

## Table of Contents

1. [Overview](#1-overview)
2. [Glossary](#2-glossary)
3. [Goals & Non-Goals](#3-goals--non-goals)
4. [Hardware Inventory](#4-hardware-inventory)
5. [Network Topology](#5-network-topology)
6. [System Architecture](#6-system-architecture)
7. [Software Stack](#7-software-stack)
8. [Functional Requirements](#8-functional-requirements)
9. [Non-Functional Requirements](#9-non-functional-requirements)
10. [Concurrency Model](#10-concurrency-model)
11. [Device Connection Specs](#11-device-connection-specs)
12. [State Machine](#12-state-machine)
13. [Vision Pipeline](#13-vision-pipeline)
14. [Data & Logging](#14-data--logging)
15. [Security & Remote Access](#15-security--remote-access)
16. [Open Questions](#16-open-questions)
17. [Revision History](#17-revision-history)
- [Appendix A — End-to-End Communication Flow](#appendix-a--end-to-end-communication-flow)
- [Appendix B — CSV Output Format](#appendix-b--csv-output-format)
- [Appendix C — David (DTM) Review Comments](#appendix-c--david-dtm-review-comments)

---

## 1. Overview

This document describes the software requirements for a lab automation cell that picks individual insect specimens from a sorting dish and places them into target wells in multi-well plates. The system is deployed in a research lab environment and is operated by lab technicians via a touchscreen HMI.

The cell integrates a SCARA robot, two controllers (one logic PLC, one safety), a syringe pump (M-Drive actuated), a pneumatic stage, two cameras, and a lighting controller — all orchestrated by a single Python application running on a Windows 11 ICOP embedded HMI computer.

Each specimen picked by the system is traceable to its bulk field sample via a field code, and to its destination via a plate ID and human-readable well position. The primary data output is a CSV file importable into Austin's lab LIMS database, with TIFF archival images per specimen.

---

## 2. Glossary

These terms are used precisely throughout this document. Use them consistently in code, UI text, and log messages.

| Term                            | Definition                                                                                                                                                                                                                                                                                                     |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Sorting dish or Sample tray** | The physical source dish/tray loaded into the machine. The robot picks specimens out of this. All specimens in one sorting dish come from the same bulk sample (same field code). Also called the "source dish" or "source tray."                                                                              |
| **Bulk sample**                 | A large collection of insects from one field collection event. One bulk sample may be too large for a single sorting dish — it may span multiple sorting dishes processed sequentially.                                                                                                                        |
| **Field code**                  | Alphanumeric identifier (e.g. `12345`) that identifies which bulk sample a specimen came from. One sorting dish has exactly one field code. All specimens picked from that sorting dish inherit that field code. Multiple sorting dishes may share the same field code if they came from the same bulk sample. |
| **Field sample**                | A sample with a distinct field code. Used interchangeably with "bulk sample" in lab context.                                                                                                                                                                                                                   |
| **Specimen**                    | One individual insect. The robot picks one specimen per cycle and places it into one well. This is the atomic unit of work.                                                                                                                                                                                    |
| **Plate**                       | A multi-well destination plate (e.g. 96-well plate) into which specimens are placed.                                                                                                                                                                                                                           |
| **Plate ID**                    | The label/identifier for a specific destination plate (e.g. `CIB-2026-001`). Entered manually by the operator in v1 (barcode scanner support planned for future). Austin expects this in the output data.                                                                                                      |
| **Plate station**               | One of the four physical nest positions where a plate can be seated in the machine. Numbered 1–4.                                                                                                                                                                                                              |
| **Well position**               | Human-readable well identifier such as `A1`, `F10`, `H12`. Comes from the plate config file. Austin explicitly requires this in the output data (not just a numeric index).                                                                                                                                    |
| **Plate config**                | A configuration file (CSV or Excel) that defines the wells in a plate and their XY positions in the plate fixture. One file covers one plate type. This is the only config file required — there is no separate load order file.                                                                               |
| **Run**                         | One execution of the sorting program from START to completion or abort. A run has a unique ID and encompasses all picks made in that session. Each run has exactly one field code — if the field code changes, the operator aborts the current run and starts a new run.                                       |
| **Cycle**                       | One pick-and-place iteration — from overhead image acquisition through specimen placement in a well. A run contains many cycles.                                                                                                                                                                               |

---

## 3. Goals & Non-Goals

### Goals

- Automate the pick-and-place of individual insect specimens from a sorting dish into multi-well plate wells with high reliability
- Provide a safe operating environment with full e-stop, light curtain, and safety contactor integration
- Give operators a clear, simple touchscreen UI to start, pause, abort, and monitor runs
- Support mid-plate field code changes: when a new sorting dish (different field code) is needed mid-plate, the operator completes the current run (CYCLE STOP → ABORT), then starts a new run with the new field code, specifying the starting well at pre-run setup to pick up where the previous run left off *(→ DTM-1)*
- Log every pick with its field code, plate ID, well position, timestamps, EoA image filename, and result in a CSV file importable into the lab's LIMS database
- Save a TIFF archival image of each specimen at the pick site (EoA camera) for the lab's records
- Support remote access for engineers via Ewon VPN
- Allow easy extension: new plate layouts, updated vision algorithms, additional hardware

### Non-Goals (v1)

- Automated barcode scanning for plate ID entry — v1 requires manual entry; barcode scanner support is a planned future enhancement (OQ-25)
- Automated recovery from hardware faults — operator must intervene to resume after any fault
- Deep learning model training or inference — the system saves EoA images to disk; what the lab does with those images (external analysis, model training, etc.) is entirely outside this system's scope
- Processing of overview (overhead) images as a lab deliverable — overhead images are retained for debugging only; Austin does not require them as a core output
- In-app config file preview or editing — config files are created and edited externally on a laptop; the HMI only displays the list of available files *(→ DTM-4)*
- Managing how config files are placed onto the HMI disk — file transfer is outside the software's scope; the software reads from the config directory regardless of how files arrived there *(→ DTM-3)*

---

## 4. Hardware Inventory

| Device                  | Model                              | Interface                                                                               | Role                                                                                                                                                                                                                                                                         | Owner (TBD)                                   |
| ----------------------- | ---------------------------------- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Robot                   | Epson T3-B SCARA                   | Ethernet / TCP                                                                          | System coordinator — pick-and-place arm; talks to syringe pump, cameras, and pneumatic stage during operation                                                                                                                                                                | Tejas (supported by Weifeng)                  |
| Logic PLC               | AutomationDirect P1-622            | Ethernet / ModbusTCP                                                                    | General I/O, interlocks, status light, work light; hardware-level event logging for troubleshooting                                                                                                                                                                          | David initially; Weifeng/Tejas for some parts |
| Safety Controller       | Sick Flexi safety controller (OQ-23) | Hardwired (safety circuit) + Ethernet/ModbusTCP (read status + write reset-acknowledge) | Hardwired: monitors light curtains, e-stop, reset button; outputs safety signal to robot and contactor. Ethernet: Python reads fault status for UI display (fault type data sourced from Flexi controller registers); Python also writes a reset-acknowledge signal when operator presses CONTINUE after a latching light curtain trip *(→ DTM-9)* | David                                         |
| Safety contactor        | (relay module)                     | Hardwired to safety controller                                                          | Cuts power to M-Drive motor on safety fault                                                                                                                                                                                                                                  | David                                         |
| Syringe pump motor      | Novanta LMDAE572C (Liberty MDrive) | Ethernet TCP port 503 — MCode ASCII (raw socket)                                        | Actuates syringe pump for specimen pick (aspirate) and place (dispense)                                                                                                                                                                                                      | Weifeng                                       |
| Pneumatic stage         | TBD                                | TBD — robot digital I/O or PLC I/O                                                      | Deploys and retracts pipette during pick-and-place; coordinated by robot                                                                                                                                                                                                     | TBD                                           |
| Overhead camera         | Lucid TRI245S-CC                   | GigE Vision / POE Ethernet — Arena SDK (arena_api)                                      | Images sorting dish to identify specimen locations                                                                                                                                                                                                                           | Tejas                                         |
| End-of-Arm (EoA) camera | Lucid TRI245S-CC                   | GigE Vision / POE Ethernet — Arena SDK (arena_api)                                      | High-resolution images at pick site; pick confirmation; TIFF archival images saved per specimen for lab records                                                                                                                                                              | Tejas                                         |
| Lighting controller     | TBD — Ethernet                     | Ethernet (ModbusTCP) or hardwired I/O — TBD (OQ-15)                                     | Controls backlight; brightness/settings adjustable over Ethernet; trigger method TBD                                                                                                                                                                                         | TBD                                           |
| HMI computer            | ICOP embedded computer             | Main switch + POE switch                                                                | Runs all software; handles image processing; interfaces with external data sources                                                                                                                                                                                           | Tejas                                         |
| VPN gateway             | Ewon Cosy+                         | Main switch / WAN                                                                       | Secure remote access for KTM (programming/troubleshooting); acts as firewall isolating internal cell network                                                                                                                                                                 | David                                         |
| Main switch             | Industrial gigabit Ethernet switch | —                                                                                       | Robot, PLC, Ewon, HMI, lighting controller, eng. laptop port                                                                                                                                                                                                                 | David                                         |
| POE switch              | Stride POE switch                  | —                                                                                       | Both cameras + HMI (co-located to minimize cross-switch load)                                                                                                                                                                                                                | David                                         |
| Eng. laptop port        | —                                  | Main switch                                                                             | Engineer access port                                                                                                                                                                                                                                                         | David                                         |
| Light curtain           | Safety light barrier               | Hardwired to safety controller                                                          | Zone entry detection — latching on trip; requires operator CONTINUE on HMI to release latch                                                                                                                                                                                  | David                                         |
| E-stop button(s)        | Mushroom head pushbutton           | Hardwired to safety controller                                                          | Manual emergency stop; button light flashes when that e-stop is active                                                                                                                                                                                                       | David                                         |
| Reset button            | Blue pushbutton                    | Hardwired to safety controller                                                          | Safety circuit reset after e-stop fault; blue light flashes when reset is available                                                                                                                                                                                          | David                                         |

> **Notes:**
> 
> - Camera model confirmed: both overhead and EoA cameras are **Lucid TRI245S-CC** (color GigE Vision, PoE). SDK: Arena SDK — Python wheel (arena_api) installed in cib_env. GigE Vision over PoE Ethernet switch confirmed (OQ-01 partially closed).
> - Safety controller brand confirmed as Sick (referred to as the "Flexi controller"); exact model TBD (OQ-23). The Ethernet interface is **not purely read-only** — Python also writes a reset-acknowledge signal to release the light curtain latch when the operator presses CONTINUE on the HMI. Python cannot override e-stop or other safety circuit states.
> - Logic PLC model: AutomationDirect P1-622 (confirmed).
> - M-Drive model confirmed: **Novanta LMDAE572C** (Liberty MDrive, LMDAM 7.008, Hw 2.10). Interface confirmed: **Ethernet TCP port 503, raw ASCII MCode** (OQ-14 closed 2026-06-18). Python uses stdlib `socket` — no Modbus, no pyserial. Programs (aspirate/dispense/home) flashed to motor non-volatile flash; Python triggers with `EX A1`, `EX A2`, `EX A3`.

---

## 5. Network Topology

There are **two separate Ethernet networks** inside the cell. The HMI connects to both. The Ewon Cosy+ acts as a firewall, isolating the internal cell network from the WAN.

```
WAN
 │
Ewon Cosy+ (VPN gateway + firewall)
 │
 ├── Main Switch (gigabit) ──────────────────────────────────────┐
 │      │                                                         │
 │    Robot (Epson T3-B)          [Ethernet]            HMI Computer (ICOP / Win 11)
 │    Logic PLC                   [Ethernet]                       │
 │    Sick Safety Controller      [Ethernet — ModbusTCP read + reset-ack write]
 │    Lighting Controller         [Ethernet — OQ-15]   POE Switch ─┤
 │    M-Drive Syringe Pump        [Ethernet TCP:503 MCode]  │         │
 │    Eng. Laptop Port                             Overhead Camera  │
 │                                                 EoA Camera       │
 └─────────────────────────────────────────────────────────────────┘

Safety circuit (hardwired — operates independently of Ethernet at all times):
  Light curtain ──┐
  E-stop button ──┼──► Sick Safety Controller ──► Safety contactor ──► M-Drive motor power
  Reset button  ──┘              │             └──► Robot safety signal input
                                  │
                    [Also on main switch via Ethernet]
                    Python reads safety status directly from Sick Flexi controller via ModbusTCP (→ DTM-5)
                    Python also writes reset-acknowledge for light curtain latch release
```

> **Two things happening on the Sick safety controller simultaneously and independently:**
> 
> 1. **Hardwired safety circuit** — reacts in milliseconds to e-stop/light curtain, stops robot and cuts motor power. No software involved.
> 2. **Ethernet ModbusTCP port** — Python reads current safety status (which fault, which e-stop, contactor state) directly from the Sick Flexi controller so the UI can show the operator what happened. Fault type details are sourced from Flexi controller Modbus registers (OQ-23). Python also writes a reset-acknowledge command to release the light curtain latch after the operator presses CONTINUE on the HMI touchscreen. *(→ DTM-5, DTM-9)*

**Key points:**

- The Ewon Cosy+ acts as a hardware firewall — no cell device has direct internet access
- The Sick safety controller circuit operates independently of software — hardware safety takes effect faster than any software path
- Motor power is cut at the hardware level by the contactor when the safety circuit trips — Python cannot override this
- Python monitors safety state by reading the Sick Flexi controller directly via ModbusTCP (100ms poll) *(→ DTM-5)*. Python can also write a reset-acknowledge for the light curtain latch — this is part of the designed protective-stop recovery flow, not an override of the safety circuit
- The Logic PLC is NOT a safety device — its job is controlling lights and logging events

---

## 6. System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    HMI Computer (Windows 11)                 │
│                                                              │
│  ┌────────────┐  ┌────────────────┐  ┌───────────────────┐   │
│  │  PyQt6 UI  │  │ State machine  │  │   Vision pipeline │   │
│  │ (Qt thread)│  │   (asyncio)    │  │   (thread pool)   │   │
│  └─────┬──────┘  └──────┬─────────┘  └─────────┬─────────┘   │
│        │  Qt signals    │  asyncio.Queue       │             │
│        └────────────────┴──────────────────────┘             │
│                         │                                    │
│          run_in_executor() worker threads                    │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐       │
│  │Robot │ │ PLC  │ │Pump  │ │Cam 1 │ │Cam 2 │ │Light │       │
│  │thread│ │thread│ │thread│ │thread│ │thread│ │thread│       │
│  └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘       │
└─────┼────────┼─────────┼────────┼─────────┼─────────┼────────┘
      │        │         │        │         │         │
   TCP/IP  ModbusTCP  TCP:503    GigE/POE GigE/POE ModbusTCP
      │        │         │        │         │         │  (or I/O)
   Epson    Logic     M-Drive  Overhead   EoA     Lighting
   T3-B      PLC      (Pump)   camera    camera   controller
```

The Python process has one `asyncio` event loop as the coordinator. All blocking hardware I/O runs in worker threads via `run_in_executor()`. The PyQt6 UI runs in the main thread with its own Qt event loop and communicates via Qt signals.

Pneumatic stage control is coordinated through the robot (digital I/O) — it does not have a dedicated software driver thread unless a separate interface is required (TBD).

---

## 7. Software Stack

| Layer                        | Technology                                                                      | Notes                                                                                                                                                                                                                                                             |
| ---------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OS                           | Windows 11                                                                      | Confirmed — ICOP embedded HMI                                                                                                                                                                                                                                     |
| Language                     | Python 3.11+                                                                    | Primary application language                                                                                                                                                                                                                                      |
| UI framework                 | PyQt6                                                                           | Touchscreen-friendly, mature                                                                                                                                                                                                                                      |
| Concurrency                  | asyncio + ThreadPoolExecutor                                                    | Event loop + thread-per-device                                                                                                                                                                                                                                    |
| State machine                | `transitions` library                                                           | Explicit states: idle → running → cycle_stop → e_stop                                                                                                                                                                                                             |
| Robot comms                  | Epson RC+ SDK / TCP socket                                                      | SPEL+ command API; robot also coordinates syringe pump and pneumatic stage                                                                                                                                                                                        |
| Logic PLC comms              | `pymodbus`                                                                      | ModbusTCP client, 100ms poll loop                                                                                                                                                                                                                                 |
| Safety controller comms      | `pymodbus`                                                                      | ModbusTCP — read safety status directly from Sick Flexi controller at 100ms; write reset-acknowledge for light curtain latch release. **Must use endian-aware decoder** (pymodbus `Endian` parameter) — known issue with byte-order from prior implementation. *(→ DTM-5)* |
| Syringe pump (M-Drive) comms | stdlib `socket` (raw TCP)                                                       | Ethernet TCP port 503, MCode ASCII. `EX A1` = aspirate, `EX A2` = dispense, `EX A3` = home. Programs stored in motor flash. No Modbus, no pyserial.                                                                                                              |
| Lighting comms               | `pymodbus` or hardwired I/O                                                     | ModbusTCP if software-triggered; robot/PLC I/O if hardware-triggered (OQ-15)                                                                                                                                                                                      |
| Camera driver                | `harvesters` + `genicam`                                                        | GigE Vision GenTL standard                                                                                                                                                                                                                                        |
| Vision / detection algorithm | OpenCV (`opencv-python`) or Halcon (MVTec) — **TBD, Tejas to evaluate (OQ-21)** | OpenCV: open source, classical contour/blob detection. Halcon: commercial, more powerful built-in tools for blob analysis, calibration, pattern matching.                                                                                                         |
| Data storage                 | `sqlite3` (stdlib)                                                              | Single file, zero admin                                                                                                                                                                                                                                           |
| CSV export                   | `csv` (stdlib) or `pandas`                                                      | Primary lab-facing output; one row per specimen pick; importable into Austin's LIMS database                                                                                                                                                                      |
| Image storage                | TIFF files (preferred); PNG acceptable                                          | TIFF is lossless — standard for scientific imaging. PNG acceptable if TIFF causes a blocker. JPEG is explicitly not acceptable (lossy compression degrades insect morphology). EoA images only — overhead images are for debugging and are not a lab deliverable. |
| Logging                      | `structlog`                                                                     | JSON-formatted, structured                                                                                                                                                                                                                                        |
| Config                       | CSV or Excel (.xlsx) — see OQ-22                                                | Plate config only (no separate load order). Format must be something non-technical lab operators can create without engineering help. CSV and Excel are the most practical. A template file should be shipped with the system.                                    |
| Packaging                    | PyInstaller or NSIS                                                             | Windows installer for deployment                                                                                                                                                                                                                                  |

---

## 8. Functional Requirements

### 8.1 Pre-Run Setup

**Operator actions (via HMI touchscreen):**

- **FR-SETUP-01:** Before starting a run, the operator shall enter:
  - **Field code** — the alphanumeric identifier for the current sorting dish (e.g. `12345`). This is the field collection identifier; all specimens picked from the current sorting dish inherit this code.
  - **Plate ID** — one text field per plate station the operator intends to activate (four fields, one per station). The operator manually types the plate label (e.g. `CIB-2026-001`). **A Plate ID must be entered before a station can be marked as active. A station without a Plate ID cannot be activated.** Barcode scanner input is a planned future enhancement (OQ-25). *(→ DTM-2)*
- **FR-SETUP-02:** The operator shall select a plate config file from a scrollable list on the HMI touchscreen. The list is automatically populated from config files already stored on the HMI disk (`C:\LabAutomation\config\plate_configs\`). There is only one config file per run — no separate load order file.
- **FR-SETUP-03:** The operator shall indicate which plate stations have plates physically seated in them by tapping the station map on the HMI touchscreen. **A station can only be marked as active after a Plate ID has been entered for it. The START button is disabled if any marked-active station has no Plate ID.** The robot will only place specimens into stations marked as active — stations without a plate are skipped entirely. Plates start empty and receive specimens during the run. *(→ DTM-2)*
- **FR-SETUP-04:** The operator may optionally specify a starting well for any plate station. This supports both new runs (default: first well in the plate config) and runs that continue mid-plate — for example, after a field code change where the operator aborted the previous run and is starting a new run to continue filling the same plates from a known well position (see FR-RUN-06). *(→ DTM-1)*
- **FR-SETUP-05:** Configuration files shall be validated at load time (schema check + checksum) and locked read-only once a run starts. If validation fails, the operator is shown a clear error message and cannot start.
- **FR-SETUP-07:** Config file format is TBD — see OQ-22. Candidate formats:
  - **CSV** — operator creates in Excel, saves as CSV. Simple column table. Python reads natively. Recommended if operators create their own configs.
  - **Excel (.xlsx)** — operator works directly in Excel, no export step. Python reads with `openpyxl`. Most natural for lab users.
  - A **template file** (Excel or CSV) shall be shipped with the system so operators know the expected column structure.

**Config file management:**

- **FR-SETUP-08:** Config files are stored in a defined folder on the HMI disk (`C:\LabAutomation\config\plate_configs\`). To add a new config, a user places a new file into this folder by any available method. No software restart required — the HMI config selection list is populated dynamically from the folder contents. *(→ DTM-3)*

- **FR-SETUP-09:** How config files are placed onto the HMI disk is **outside the scope of this software**. The software reads from `C:\LabAutomation\config\plate_configs\` and does not manage how files arrive there. Engineers may use any method available to them (direct access, network share, USB, etc.) to place files in that directory. *(→ DTM-3)*

- **FR-SETUP-10:** Any user (operator or engineer) shall be able to view the list of existing config files and delete a selected file from the HMI config management screen. The config management screen displays filenames only — there is no in-app file preview or editing capability. Config files are created and modified externally on a laptop. *(→ DTM-4)*

### 8.2 Robot Control

- **FR-ROB-01:** Software shall connect to the Epson T3-B controller over TCP on startup and verify connection before allowing any run.
- **FR-ROB-05:** Robot connection loss shall trigger an immediate cycle stop and operator alert.
- **FR-ROB-06:** The robot shall coordinate the syringe pump and pneumatic stage during pick-and-place execution.

### 8.3 Logic PLC

> **What David says this device does:** *"controller for a few components such as the status light and work light... also act as a data logger for hardware-level event logging for troubleshooting and debugging."*
> The Logic PLC is **not a safety device**. It does not sit between Python and the Sick safety controller for safety monitoring. Its job is outputs (lights) and logging.

- **FR-PLC-01:** Software shall connect to the Logic PLC over ModbusTCP on startup and poll it at 100ms intervals during all operating states.
- **FR-PLC-02:** Software shall write to the Logic PLC to control the cell status light and work light (e.g. green = running, amber = stopped, red = fault).
- **FR-PLC-03:** Software shall read the PLC hardware event log and store entries in the structured application log for traceability.
- **FR-PLC-04:** PLC connection loss shall be logged and shall prevent a new run from starting (since light/status control would be unavailable).

> **What FR-PLC does NOT cover:** Safety status monitoring (e-stop, light curtain, contactor state). That is the Sick safety controller's job — see Section 8.4.

### 8.4 Safety Circuit (Sick Safety Controller)

> **Two separate interfaces on the Sick Flexi safety controller:**
> 
> - **Hardwired (safety circuit):** Light curtain, e-stop buttons, reset button wired IN. Robot safety signal, motor contactor wired OUT. Operates entirely in hardware — independently of software, faster than any software can react. Python has zero control over this path.
> - **Ethernet / ModbusTCP:** Python reads safety status (which e-stop is active, light curtain state, contactor state, etc.) **directly from the Sick Flexi controller** at 100ms intervals. Fault type detail (which e-stop button, light curtain vs. contactor) is sourced from the Flexi controller's Modbus registers (register map required — OQ-23). Python also **writes** a reset-acknowledge command to release the light curtain latch when the operator presses CONTINUE on the HMI touchscreen. This is the designed protective-stop recovery mechanism — not an override of safety. *(→ DTM-5, DTM-9)*
> 
> **Light curtain latching behavior (confirmed by David):** When the light curtain trips, it **latches** — it stays in the faulted/open state even after the person steps back out of the zone. This is intentional: if it auto-reset the moment the person left, the machine could restart unexpectedly. To resume after a light curtain trip, the operator must press CONTINUE on the HMI. The software then sends a reset-acknowledge signal to the safety controller, which releases the latch and allows the circuit to close. The robot may then resume.
> 
> **OQ-12 — CLOSED:** Python reads safety status directly from the Sick Flexi controller via ModbusTCP. The Logic PLC is not in this path. The `pymodbus` implementation must use an endian-aware decoder (the `Endian` parameter in pymodbus) — a byte-order issue was encountered in a prior implementation using a different library. *(→ DTM-5)*
> 
> **OQ-23 (open):** Sick safety controller exact model + Modbus register map for read (status) and write (reset-acknowledge). Required before implementing the safety driver.

The following are software monitoring and control requirements. The safety circuit acts in hardware regardless of software state.

- **FR-SAF-01:** Software shall read safety status directly from the Sick Flexi controller (via ModbusTCP) at 100ms intervals. *(→ DTM-5)*
- **FR-SAF-02:** Software shall detect and log: light curtain trip, e-stop button activation (which button), reset button press, contactor state change.
- **FR-SAF-03:** When a safety fault is detected via the 100ms poll, software shall immediately transition the state machine to `E_STOP` or `PROTECTIVE_STOP` (depending on fault type) and block all further motion commands. **This is a software mirror state only — the hardware safety circuit has already acted independently before this transition occurs. The software state machine transition does not cause the hardware stop; it reflects it.** *(→ DTM-6)*
- **FR-SAF-04:** The system shall NOT allow a run to start if any active safety fault is detected. This is a software-level gate that provides operator feedback; the hardware independently enforces this regardless — even if software attempted to start a run while a safety fault is active, the hardware safety system would prevent any motion. *(→ DTM-7)*
- **FR-SAF-05:** After an e-stop, the hardware reset sequence must fully complete in hardware (e-stop button released → blue reset button pressed and released) before software re-enables the START/CONTINUE button. Software detects this by polling the Sick Flexi controller status. **Additionally, after the e-stop hardware reset sequence completes, the software shall also send a light curtain circuit reset-acknowledge to the Sick controller to ensure the light curtain latch is released (exact register and sequence pending OQ-23 register map confirmation).** *(→ DTM-8)*
- **FR-SAF-06:** After hardware reset is confirmed (e-stop) or latch released (light curtain), software shall additionally require an explicit operator action (START/CONTINUE button press on HMI) before motion resumes. Software never resumes autonomously.
- **FR-SAF-07:** Software shall display the specific fault type on the UI — which e-stop button was pressed, or light curtain, or contactor — not just a generic "safety fault." This information is read from the Sick Flexi controller's Modbus registers (register map required — OQ-23). *(→ DTM-9)*
- **FR-SAF-08:** Light curtain mode is **latching protective stop**. When the light curtain trips:
  - Hardware immediately stops the robot and opens the contactor (no software involved).
  - Software detects the trip via the 100ms safety poll.
  - State machine transitions to PROTECTIVE_STOP.
  - UI shows PROTECTIVE_STOP banner. The **CONTINUE button is disabled (grayed out) until the light curtain is physically clear** (no obstruction detected by the 100ms poll). The ABORT button is available at any time. This prevents the operator from attempting to reset the system while their arm or any obstruction is still in the cell. *(→ DTM-10)*
  - Once the light curtain reads clear and the operator presses CONTINUE, software sends a reset-acknowledge write to the Sick safety controller via ModbusTCP.
  - Safety controller releases the latch, circuit closes, contactor closes, robot ready.
  - State machine transitions back to RUNNING; cycle resumes.
  - No physical hardware button pressing required (unlike e-stop reset).
- **FR-SAF-09:** Software shall send the reset-acknowledge write to the Sick safety controller only when: (a) the light curtain is physically clear (no obstruction detected — confirmed by live 100ms poll), AND (b) the operator has explicitly pressed CONTINUE on the HMI. The CONTINUE button shall reflect the light curtain status in real time — it is disabled while any obstruction is detected. Software shall not send this signal automatically. *(→ DTM-10)*

> **Hardware behavior David described — e-stop (for UI reference):** When an e-stop button is pressed, its indicator light on the physical hardware button flashes (tells you which one is active). Other e-stop buttons that are not pressed have their light lit steady. Once the pressed button is released and nothing else is blocking reset, the blue reset button light starts flashing. Operator presses and releases the blue reset button → safety controller returns to operational mode → safety contactor closes → robot safety signal restored. Software must reflect this state progression in the UI.

### 8.5 Syringe Pump (M-Drive)

- **FR-PUMP-01:** Software shall connect to the M-Drive on startup and verify connection before allowing any run.
- **FR-PUMP-02:** On robot command during pick: software shall command the M-Drive to aspirate (suck up specimen).
- **FR-PUMP-03:** On robot command during place: software shall command the M-Drive to dispense (expel specimen into well).
- **FR-PUMP-04:** M-Drive faults or contactor-open state (power cut by safety circuit) shall trigger a cycle stop.
- **FR-PUMP-05:** Software shall detect M-Drive communication loss and trigger a cycle stop.
- **FR-PUMP-06:** Aspirate and dispense parameters (volume, speed) shall be configurable in the plate config or a separate machine config.

### 8.6 Vision — Overhead Camera

- **FR-VIS-01:** Software shall acquire a full-resolution frame from the overhead camera (backlight ON) at the start of each cycle.
- **FR-VIS-02:** Vision pipeline shall detect all individual specimens in the frame and return pixel centroids.
- **FR-VIS-03:** Pixel centroids shall be converted to robot coordinate space using a calibration transform.
- **FR-VIS-04:** If zero specimens are detected, software shall alert operator and await instruction.
- **FR-VIS-05:** The most recently acquired overhead image shall be reused in step 2 of each cycle rather than triggering a new image when the robot already has the latest view (see operating cycle step 9).
- **FR-VIS-06:** Overhead images are saved to disk for debugging purposes only. They are not a primary lab deliverable and are not included in the CSV output.

### 8.7 Vision — EoA Camera

- **FR-EOA-01:** Software shall acquire one or more frames from the EoA camera after the robot moves to the pick site (before pick execution). The number of images and lighting variants per pick is TBD — OQ-24.
- **FR-EOA-02:** The EoA image(s) shall be analyzed to determine: (a) whether to proceed with pick; (b) refined pick coordinates.
- **FR-EOA-03:** If the vision analysis determines no pick should be made, the robot shall move clear and return to step 1 of the operating cycle. Software shall support a configurable number of alternate coordinates to try before returning.
- **FR-EOA-04:** Pick confirmation (post-pick EoA image) shall determine pass/fail — this is optional per OQ-04.
- **FR-EOA-05:** Pick result shall be logged with timestamps, pick coordinates, and pass/fail status.
- **FR-EOA-06:** Configurable number of consecutive pick failures shall trigger a cycle stop and operator alert.
- **FR-EOA-07:** All EoA images shall be saved to disk in TIFF format (PNG acceptable if TIFF blocked; JPEG not acceptable). These are the primary archival deliverable to the lab. Filename(s) shall be included in the CSV output row for that specimen.

> **Camera trigger:** EoA camera may be triggered via software (GigE) or via robot digital I/O. Method TBD — see OQ-03.

### 8.8 Lighting Controller

- **FR-LIT-01:** Software shall command backlight ON before overhead camera capture and OFF after frame acquisition.
- **FR-LIT-02:** Lighting trigger timing shall be configurable (lead time before capture in ms).
- **FR-LIT-03:** Strobe mode (if supported by hardware) shall be configurable per camera.
- **FR-LIT-04:** Lighting controller connection loss shall be logged and shall prevent a new run from starting.
- **FR-LIT-05:** The trigger method (ModbusTCP software command vs. hardwired I/O from robot/PLC) is TBD — see OQ-15. The software architecture shall abstract lighting trigger so the method can be changed without restructuring the operating cycle.

### 8.9 Operator UI

**Pre-run setup screen (operator, via touchscreen):**

- **FR-UI-01:** UI shall provide a pre-run setup screen with the following fields before a run can start:
  - **Field code** — text field for the current sorting dish's field collection code (e.g. `12345`)
  - **Plate IDs** — four text fields (one per plate station), each labeled with the station number. Operator types the plate label (e.g. `CIB-2026-001`). **A Plate ID must be entered for a station before that station can be activated on the station map.** *(→ DTM-2)*
  - **Plate config** — dropdown/scrollable list populated from `C:\LabAutomation\config\plate_configs\` on HMI disk
  - **Station map** — visual diagram of the four plate station nests; operator taps each nest that has an empty plate physically seated in it. **A station tap is only accepted if a Plate ID has already been entered for that station — tapping a station with no Plate ID shows an inline prompt to enter the Plate ID first.** *(→ DTM-2)*
  - **Starting well (optional)** — per-station field to specify which well to begin from (for runs that continue mid-plate after a field code change; defaults to first well for new runs) *(→ DTM-1)*
  - **START** button — active only once all required fields are filled and valid (field code entered, at least one station active with a Plate ID, config file selected)
- **FR-UI-02:** The plate config selection list shall be populated dynamically from the files present in the HMI config folder. No restart required when new configs are added.

**Run control buttons:**

- **FR-UI-03 — Stopped state:** When the cell is not running, UI shall show **START** and **ABORT** buttons.
  - **START** begins processing (or resumes after a cycle stop — may be labeled **CONTINUE** in that context).
  - **ABORT** clears all in-progress run data and returns to the pre-run setup screen, ready for a new run.
- **FR-UI-04 — Running state:** When the cell is running, UI shall show a **CYCLE STOP** button. Pressing it allows the current cycle to complete gracefully, then stops. Once stopped, **CONTINUE** and **ABORT** buttons become active. To change the field code, the operator presses ABORT and starts a new run from the pre-run setup screen. *(→ DTM-1)*
- **FR-UI-05 — Light curtain trip (protective stop with latch):** When the light curtain is tripped during a run:
  - Robot stops immediately (hardware).
  - UI shows a PROTECTIVE STOP banner indicating the light curtain is the cause.
  - UI shows **ABORT** button (always available) and **CONTINUE** button.
  - **The CONTINUE button is disabled (grayed out) as long as the light curtain detects any obstruction. It becomes enabled only when the 100ms safety poll confirms the light curtain is physically clear.** This prevents reset attempts while someone is still in the cell. *(→ DTM-10)*
  - Once CONTINUE is enabled and the operator presses it, software sends a reset-acknowledge signal to the Sick safety controller via ModbusTCP.
  - Safety controller releases the light curtain latch; circuit closes.
  - State machine transitions back to RUNNING; cycle resumes.
  - The machine does NOT resume automatically when the person leaves the zone — operator CONTINUE is always required.
- **FR-UI-06 — E-stop state:** UI shall display a prominent alert showing the specific e-stop source (data from Flexi controller registers — OQ-23). The operator must complete the hardware reset procedure (release e-stop button → press blue reset button) before software enables the CONTINUE button. Software detects reset completion by polling safety controller status. *(→ DTM-9)*
- **FR-UI-15 — Plate ID entry:** Plate ID fields on the pre-run screen accept manual text input in v1. Future: tapping a plate ID field may activate a barcode scanner input mode (OQ-25). Austin's team will design plate labels to accommodate whatever input method works best.

**Run monitoring:**

- **FR-UI-07:** UI shall display current system state prominently (IDLE / RUNNING / CYCLE STOPPING / E-STOP / PROTECTIVE STOP / FAULT).
- **FR-UI-08:** UI shall display live overhead camera feed during a run.
- **FR-UI-09:** UI shall display current specimen count (picked), pass count, fail count, elapsed time, and current field code.
- **FR-UI-10:** UI shall display the last 50 log entries in a scrollable panel.

**Engineer screens:**

- **FR-UI-11:** UI shall support two roles: Operator and Engineer. Both roles have access to pre-run setup, run control, monitoring, and config management. The Engineer role additionally has access to calibration and system-level settings.
- **FR-UI-12:** The config management screen shall be accessible to both operators and engineers. It shall display the list of plate config filenames stored on the HMI. Any user can select a file from the list and delete it. There is no in-app preview or editing of config file contents — files are created and modified externally on a laptop. *(→ DTM-4)*
- **FR-UI-13:** The calibration screen (running and saving camera-to-robot calibration) is accessible to the Engineer role only.

### 8.10 Run Management

- **FR-RUN-01:** Each run shall have a unique ID (timestamp-based) used as a key for all logged data and images.

- **FR-RUN-02:** Operator shall enter the field code for the current sorting dish and a Plate ID for each station they intend to activate before starting a run. A station cannot be activated without a Plate ID. *(→ DTM-2)*

- **FR-RUN-03:** Any user shall load a plate config before starting a run.

- **FR-RUN-04:** Config files shall be read-only (locked) once a run starts.

- **FR-RUN-05:** Abort shall clear all in-progress run state and data, returning the system to IDLE ready for a new run.

- **FR-RUN-06 — Mid-plate field code change:** When an operator needs to change the field code mid-plate (e.g. when switching to a new sorting dish with a different field code while the destination plates are still partially filled): *(→ DTM-1)*
  
  1. Operator presses CYCLE STOP — run stops after the current pick cycle completes.
  
  2. Operator presses ABORT — the current run ends, run data is saved, system returns to pre-run setup screen.
  
  3. Operator loads the new sorting dish into the machine.
  
  4. On the pre-run setup screen, operator enters the new field code.
  
  5. Operator enters a Plate ID for each station (same plate IDs as before, or new ones if plates were swapped).
  
  6. Operator activates the plate stations on the station map.
  
  7. Operator specifies a starting well for each active station to continue from where the previous run left off.
  
  8. Operator presses START — a new run begins, tagged with the new field code, from the specified starting wells.
  
  Each field code segment is a separate run in the data (distinct run ID, separate CSV file). This keeps data clean and logging simple.

- **FR-RUN-07:** The well fill sequence shall be tracked per plate station within a run. The software shall know which well within each active station is next in the current run, and shall correctly begin from the operator-specified starting well at run setup.

---

## 9. Non-Functional Requirements

| ID     | Category        | Requirement                                                                                                                                                                                                                                                                                                                             |
| ------ | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NFR-01 | Safety          | Software e-stop response time (Sick safety fault detected via ModbusTCP poll → robot halt command sent by software): < 200ms. Note: the hardware safety circuit (Sick controller → robot safety signal, hardwired) acts independently in <10ms — software response is a secondary layer for UI/logging, not the primary stop mechanism. |
| NFR-02 | Reliability     | System shall run continuously for 8-hour shifts without requiring restart                                                                                                                                                                                                                                                               |
| NFR-03 | Performance     | Full vision detection cycle (capture → centroid output) shall complete in < 2 seconds                                                                                                                                                                                                                                                   |
| NFR-04 | Performance     | Logic PLC and Sick safety controller poll cycles shall both maintain 100ms period under full system load                                                                                                                                                                                                                                |
| NFR-05 | Usability       | Operator shall be able to start a standard run within 3 interactions from the home screen                                                                                                                                                                                                                                               |
| NFR-06 | Maintainability | All device drivers shall be isolated in separate modules with no cross-dependencies                                                                                                                                                                                                                                                     |
| NFR-07 | Security        | No device on the cell network shall have direct internet access                                                                                                                                                                                                                                                                         |
| NFR-08 | Security        | Remote access exclusively via Ewon Cosy+ VPN — no direct RDP/SSH exposure                                                                                                                                                                                                                                                               |
| NFR-09 | Auditability    | Every operator action and system event shall be timestamped and written to the structured log                                                                                                                                                                                                                                           |
| NFR-10 | Recoverability  | After an e-stop, operator shall be able to resume (re-home + re-detect) without restarting software                                                                                                                                                                                                                                     |
| NFR-11 | Platform        | Application shall run on Windows 11 and be packaged as a Windows installer                                                                                                                                                                                                                                                              |
| NFR-12 | Data integrity  | CSV export shall be written incrementally per pick (not at run end) so partial data is preserved if the system faults mid-run                                                                                                                                                                                                           |

---

## 10. Concurrency Model

The application uses a **single asyncio event loop** as the coordinating brain, with **one worker thread per blocking hardware device**.

```
asyncio event loop
├── poll_safety()         — await run_in_executor → pymodbus read/write → Sick Flexi controller (100ms) (→ DTM-5)
│                           reads: e-stop/light curtain/contactor state + fault type registers
│                           writes: reset-acknowledge on operator CONTINUE after light curtain latch
│                           also: light curtain circuit reset after e-stop recovery sequence (→ DTM-8)
├── poll_plc()            — await run_in_executor → pymodbus read/write → Logic PLC (100ms) — status light + work light + event log
├── poll_robot_status()   — await run_in_executor → TCP recv → Epson T3-B (200ms)
├── pump_command()        — await run_in_executor → M-Drive write/read (TBD interface)
├── overhead_cam_loop()   — await run_in_executor → harvesters grab + OpenCV
├── eoa_cam_loop()        — await run_in_executor → harvesters grab + OpenCV
├── lighting_command()    — await run_in_executor → pymodbus write (or robot I/O — TBD)
└── state_machine()       — await asyncio.Queue for events from all threads
                            drives all decision logic

PyQt6 UI (main thread, separate Qt event loop)
└── receives state updates via Qt signals from asyncio loop
    sends operator commands via thread-safe queue
    CONTINUE button enabled/disabled based on live light_curtain_clear flag from safety poll (→ DTM-10)
```

Rules:

- All business logic lives in the asyncio event loop — never in worker threads
- Worker threads only do I/O and put results onto `asyncio.Queue`
- The state machine is the only consumer of the event queue
- PyQt6 UI communicates to asyncio via `loop.call_soon_threadsafe()`

---

## 11. Device Connection Specs

### 11.1 Epson T3-B Robot

| Parameter            | Value                                   |
| -------------------- | --------------------------------------- |
| Network              | Main switch                             |
| Protocol             | TCP/IP                                  |
| Port                 | TBD (RC+ default: 5007)                 |
| Library              | Epson RC+ SDK or raw socket — see OQ-07 |
| Reconnect on failure | Yes — 3 retries then fault              |
| Poll rate            | 200ms status read during run            |

### 11.2 Logic PLC

| Parameter     | Value                                                                                                                                                                                                           |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Model         | AutomationDirect P1-622                                                                                                                                                                                         |
| Network       | Main switch                                                                                                                                                                                                     |
| Protocol      | ModbusTCP                                                                                                                                                                                                       |
| Port          | 502                                                                                                                                                                                                             |
| Library       | `pymodbus` 3.x                                                                                                                                                                                                  |
| Poll rate     | 100ms                                                                                                                                                                                                           |
| Key registers | Status light output, work light output, hardware event log — see OQ-11. Safety state (e-stop, light curtain, contactor) is read from the Sick safety controller directly, not from this PLC — see Section 11.3. |

### 11.3 Safety Controller (Sick Flexi)

| Parameter         | Value                                                                                                             |
| ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| Model             | Sick Flexi safety controller — exact model TBD (OQ-23)                                                            |
| Primary interface | Hardwired — safety circuit inputs (light curtain, e-stop, reset) and outputs (contactor, robot safety signal)     |
| Network interface | ModbusTCP on main switch — **Python reads directly from this device** (OQ-12 resolved — see DTM-5)               |
| Software reads    | Light curtain state, e-stop button states (individual), reset button state, contactor state, fault type registers — 100ms poll |
| Software writes   | Reset-acknowledge command to release light curtain latch when operator presses CONTINUE on HMI (FR-SAF-08/09); additionally light curtain circuit reset after e-stop recovery (FR-SAF-05) *(→ DTM-8)* |
| Register map      | TBD — OQ-23                                                                                                       |
| Implementation note | Use endian-aware pymodbus decoder (`Endian` parameter) — byte-order issue encountered in prior implementation *(→ DTM-5)* |

### 11.4 Syringe Pump Motor (Novanta LMDAE572C)

| Parameter       | Value                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------- |
| Model           | Novanta LMDAE572C — Liberty MDrive, LMDAM 7.008, Hardware 2.10                                 |
| IP / Port       | 172.16.9.16 : 503                                                                              |
| Interface       | Ethernet TCP port 503 — raw ASCII MCode (stdlib `socket`, CR-terminated commands)              |
| Library         | Python stdlib `socket` — no external library required                                          |
| Role            | Actuates syringe pump: aspirate (suck) to pick specimen, dispense (expel) to place into well   |
| Commands        | `EX A1` = aspirate, `EX A2` = dispense, `EX A3` = return to home position                     |
| Program storage | Programs stored in motor non-volatile flash — survive power cycles                             |
| Steps/rev       | 51,200 (200 full steps × 256 microsteps). Tune per syringe mechanics once confirmed.           |
| Power interlock | Motor power cut by safety contactor — software cannot command pump when safety circuit is open |

### 11.5 Pneumatic Stage

| Parameter       | Value                                       |
| --------------- | ------------------------------------------- |
| Interface       | TBD — likely robot digital I/O or PLC I/O   |
| Control         | Coordinated by robot during operating cycle |
| Software access | Via robot SPEL+ commands or PLC I/O write   |

### 11.6 Cameras (Both)

| Parameter | Value                                                                  |
| --------- | ---------------------------------------------------------------------- |
| Network   | POE switch (separate from main switch)                                 |
| Protocol  | GigE Vision (GenTL)                                                    |
| Library   | `harvesters` + `genicam`                                               |
| Trigger   | Software trigger from Python (GigE) or robot digital I/O — TBD (OQ-03) |
| Format    | Mono8 or BayerRG8 (TBD on sensor)                                      |
| Output    | numpy array → OpenCV → TIFF to disk (EoA); TIFF debug only (overhead)  |

### 11.7 Lighting Controller

| Parameter | Value                                                                                    |
| --------- | ---------------------------------------------------------------------------------------- |
| Network   | Main switch                                                                              |
| Protocol  | ModbusTCP (Ethernet) — software-triggered; or hardwired I/O from robot/PLC — TBD (OQ-15) |
| Library   | `pymodbus` 3.x (if software trigger)                                                     |
| Channels  | Backlight (overhead), ring light / strobe (EoA) — channel map TBD (OQ-13)                |

---

## 12. State Machine

States and allowed transitions:

```
INITIALIZING    → IDLE
IDLE            → RUNNING          (operator presses START after pre-run setup complete)
RUNNING         → CYCLE_STOPPING   (operator presses CYCLE STOP — finishes current cycle then stops)
RUNNING         → E_STOP           (e-stop button pressed — hardware + software)
RUNNING         → PROTECTIVE_STOP  (light curtain tripped — latching; operator must press CONTINUE)
RUNNING         → FAULT            (connection loss, unrecoverable error)
CYCLE_STOPPING  → IDLE             (after safe robot park)
IDLE            → RUNNING          (operator presses CONTINUE after cycle stop)
IDLE            → IDLE             (operator presses ABORT — clears run data, returns to pre-run setup screen)
E_STOP          → IDLE             (hardware reset sequence complete + operator presses CONTINUE)
PROTECTIVE_STOP → RUNNING          (light curtain clear + operator presses CONTINUE → software resets latch → circuit closes)
FAULT           → IDLE             (operator clears, software restart may be required)
```

> **Mid-plate field code change flow (→ DTM-1):** There is no "CHANGE FIELD CODE" state or button. To change the field code mid-plate: CYCLE STOP → IDLE → ABORT → pre-run setup screen → enter new field code and starting well → START (new run).

### State Definitions

| State           | Robot                          | Pump                    | Cameras    | Logic PLC Poll (lights/log) | Safety Poll (Sick Flexi) | UI Buttons Active                                   |
| --------------- | ------------------------------ | ----------------------- | ---------- | --------------------------- | ------------------------ | --------------------------------------------------- |
| INITIALIZING    | Connecting                     | Connecting              | Connecting | Starting                    | Starting                 | None                                                |
| IDLE            | Parked                         | Standby                 | Standby    | Active                      | Active                   | START, ABORT                                        |
| RUNNING         | Active                         | On-demand               | Active     | Active                      | Active                   | CYCLE STOP                                          |
| CYCLE_STOPPING  | Finishing cycle then parking   | Finishing cycle         | Off        | Active                      | Active                   | (none — waiting for cycle to complete)              |
| E_STOP          | Halted (HW safety also active) | Halted (contactor open) | Off        | Active                      | Active                   | CONTINUE + ABORT (only after HW reset done)         |
| PROTECTIVE_STOP | Halted (light curtain latched) | Halted (contactor open) | Off        | Active                      | Active                   | CONTINUE (disabled until light curtain clear *(→ DTM-10)*), ABORT |
| FAULT           | Halted                         | Halted                  | Off        | Best-effort                 | Best-effort              | ABORT                                               |

After CYCLE_STOPPING → IDLE, the operator sees CONTINUE and ABORT. To change the field code, the operator presses ABORT to return to pre-run setup, then starts a new run with the new field code. *(→ DTM-1)*

---

## 13. Vision Pipeline & Main Operating Cycle

### 13.1 Main Operating Cycle

The following is the nominal per-specimen operating cycle once a run is started:

```
1.  Overhead camera acquires a frame (backlight ON → capture → backlight OFF).
2.  Software analyzes image and determines the next specimen to pick (location in sorting dish).
3.  Software sends robot a "mission": move to pick coordinates and take a close-up image.
4.  Robot moves to pick site; EoA camera captures one or more images (OQ-24).
5.  Software analyzes EoA image(s):
      a. Determines whether to proceed with pick.
      b. Refines pick coordinates.
6.  If NO pick: robot moves clear → return to step 1.
    If more coordinates to try: retry at alternate location before returning to step 1.
    If YES pick: continue to step 7.
7.  Software sends robot a "mission": pick from refined coordinates,
    place into next available well (coordinates from plate config).
8.  Robot executes mission:
      a. Move to safe height.
      b. Deploy pipette (pneumatic stage extends via robot digital I/O).
      c. Move to pick position.
      d. Command syringe pump → aspirate specimen.
      e. [Optional] EoA pick confirmation image — see OQ-04.
      f. Move to place position (target well in multi-well plate).
      g. Once robot is outside overhead camera FoV, trigger next overhead image acquisition.
      h. Command syringe pump → dispense specimen into well.
      i. Retract pipette (pneumatic stage retracts via robot digital I/O).
9.  Log pick result: field code, plate ID, plate station, well position, image filename(s),
    timestamp_start, timestamp_end, result, error (if any). Append row to CSV.
10. Return to step 2, using the overhead image already acquired in step 8g.
```

### 13.2 Overhead Detection (v1 — OpenCV)

```
1. Command lighting controller: backlight ON
2. Wait configurable lead time (ms)
3. Acquire frame from overhead camera (software trigger via GigE)
4. Command lighting controller: backlight OFF
5. Save raw frame to TIFF (run_id/overhead/frame_NNN.tiff) — for debugging only
6. Resize to processing resolution (configurable, default 25%)
7. Gaussian blur → background subtraction → threshold
8. Morphological open (denoise) + close (merge fragments)
9. Contour detection → filter by area (min/max configurable)
10. Compute centroids at full resolution
11. Apply calibration homography → robot XY coordinates
12. Return centroid list to state machine
```

Key tuning parameters (in machine config):

- `detection.scale` — processing downscale factor (default 0.25)
- `detection.threshold` — background subtraction threshold (default 15)
- `detection.min_area_px` — minimum contour area to accept
- `detection.max_area_px` — maximum contour area to accept
- `lighting.backlight_lead_ms` — delay between light on and camera trigger

### 13.3 EoA Close-Up Analysis (v1)

- Acquire one or more frames at pick site (number and lighting variants — OQ-24)
- Analyze to determine: proceed with pick (yes/no) and refined XY coordinates
- If pick confirmation enabled: post-pick image checked for specimen presence in pipette ROI
- All EoA images saved to disk in TIFF format — these are the primary archival deliverable to the lab
- Image filename(s) included in the CSV output row for that specimen

---

## 14. Data & Logging

### 14.1 Directory Structure

```
C:\LabAutomation\data\
  runs\
    {run_id}\
      overhead\     — raw TIFF frames (overhead camera, debugging only — not a lab deliverable)
      eoa\          — EoA TIFF images per specimen (primary archival deliverable)
      {run_id}_picks.csv   — CSV export for this run (lab LIMS import file)
  picks.db          — SQLite: all runs, picks, safety events
  logs\
    app_{date}.jsonl   — structured JSON log (structlog)
  config\
    plate_configs\  — plate config files (CSV or Excel, read-only during runs)
    calibration\    — homography matrices (numpy .npy)
```

> **Notes:**
> 
> - The `load_orders\` folder has been removed — there is no separate load order config. Well fill sequence is defined within the plate config file.
> - The CSV file is the primary lab-facing output. The SQLite database is the internal audit trail.
> - Overhead images are retained for debugging purposes only. They are not delivered to the lab or included in the CSV.
> - Each run has exactly one field code. Mid-plate field code changes result in a new run with a new run ID and a new CSV file. *(→ DTM-1)*

### 14.2 Database Schema (SQLite)

**runs table**

| Column      | Type     | Notes                                              |
| ----------- | -------- | -------------------------------------------------- |
| run_id      | TEXT PK  | Timestamp-based unique ID (e.g. `20260605_142250`) |
| started_at  | DATETIME |                                                    |
| ended_at    | DATETIME | Null if incomplete                                 |
| status      | TEXT     | completed / aborted / fault                        |
| config_file | TEXT     | Name of plate config used                          |
| operator    | TEXT     | Role/user identifier                               |

**picks table**

| Column          | Type       | Notes                                                                         |
| --------------- | ---------- | ----------------------------------------------------------------------------- |
| pick_id         | INTEGER PK |                                                                               |
| run_id          | TEXT FK    |                                                                               |
| field_code      | TEXT       | Field collection identifier for this specimen (e.g. `12345`)                  |
| plate_id        | TEXT       | Operator-entered plate label (e.g. `CIB-2026-001`)                            |
| plate_position  | INTEGER    | Which plate station (1–4)                                                     |
| well_position   | TEXT       | Human-readable well identifier (e.g. `A1`, `H12`) — from plate config         |
| robot_x         | REAL       | Commanded pick X coordinate                                                   |
| robot_y         | REAL       | Commanded pick Y coordinate                                                   |
| timestamp_start | DATETIME   | When this pick cycle began                                                    |
| timestamp_end   | DATETIME   | When this pick cycle completed (or failed)                                    |
| result          | TEXT       | success / failed_pick / skipped / aborted                                     |
| error_code      | TEXT       | Machine-readable error identifier if result is not success (null if success)  |
| error_message   | TEXT       | Human-readable error detail (null if success)                                 |
| image_filenames | TEXT       | JSON array of EoA TIFF filenames for this pick (may be more than one — OQ-24) |
| overhead_frame  | TEXT       | Path to overhead TIFF for this cycle (debugging reference only)               |

**safety_events table**

| Column     | Type       | Notes                                               |
| ---------- | ---------- | --------------------------------------------------- |
| event_id   | INTEGER PK |                                                     |
| run_id     | TEXT FK    | Null if not during a run                            |
| timestamp  | DATETIME   |                                                     |
| event_type | TEXT       | estop / light_curtain / contactor / connection_loss |
| detail     | TEXT       | Free text detail                                    |

### 14.3 CSV Export

The CSV file is the primary output for Austin's lab. One file per run, appended row-by-row as picks complete (not written at run end — see NFR-12). See Appendix B for the exact column specification.

CSV files are stored at `C:\LabAutomation\data\runs\{run_id}\{run_id}_picks.csv`.

### 14.4 Structured Log Fields

Every log entry (via `structlog`) includes:

```json
{
  "timestamp": "2026-06-05T14:23:01.123Z",
  "level": "info",
  "event": "pick_completed",
  "run_id": "20260605_142250",
  "pick_id": 42,
  "field_code": "12345",
  "plate_id": "CIB-2026-001",
  "plate_position": 2,
  "well_position": "A1",
  "result": "success",
  "duration_ms": 1840
}
```

---

## 15. Security & Remote Access

- Cell devices on isolated subnet behind Ewon Cosy+ firewall — no direct internet access
- HMI is the only machine with limited outbound access (data export path TBD — OQ-05)
- Remote access: Ewon Cosy+ VPN only — KTM engineers can access the system as if directly connected
- VPN access restricted to named engineer accounts
- HMI authentication: minimum two roles (Operator / Engineer) with PIN or Windows login. Both roles can manage configs (view list and delete plate configs). Engineer role additionally has access to calibration and system-level settings. *(→ DTM-4)*
- Config files validated by checksum at load time
- No USB auto-run; data transfer via defined network path only
- Windows Update and antivirus scheduled to maintenance windows only
- Software updates tested on staging environment before production deployment

---

## 16. Open Questions

| #     | Question                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Owner            | Priority | Status                                                                                                                                                                                                                                                                                                                                                     |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OQ-01 | Final camera model selection — affects GigE GenTL driver specifics                                                                                                                                                                                                                                                                                                                                                                                                                                | Hardware team    | High     | **PARTIALLY CLOSED: Both cameras confirmed as Lucid TRI245S-CC (color GigE Vision). SDK and software available at https://thinklucid.com/downloads-hub/ — GigE Vision GenTL driver selection can proceed. Arena SDK (arena_api Python wheel) confirmed working on HMI.**                                                                                   |
| OQ-02 | ~~Lighting controller — Modbus or GPIO?~~                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Hardware team    | High     | **CLOSED: Ethernet (ModbusTCP) confirmed; hardware I/O trigger also possible (see OQ-15)**                                                                                                                                                                                                                                                                 |
| OQ-03 | EoA camera trigger method — software trigger via GigE, or robot digital I/O line? Affects pick confirmation timing                                                                                                                                                                                                                                                                                                                                                                                | SW + HW          | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-04 | Pick confirmation strategy — EoA post-pick image, overhead cross-check, or skip? How to handle a failed pick?                                                                                                                                                                                                                                                                                                                                                                                     | Lab / SW         | Medium   | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-05 | External data export — **CSV confirmed** as primary format for Austin's LIMS import. **TIFF confirmed** as image format (PNG acceptable; JPEG not acceptable). **Exact CSV columns confirmed** — see Appendix B. Remaining: (a) Does Austin want a separate export trigger on HMI, or is the CSV simply available on disk? (b) FTP/REST/USB delivery path TBD. (c) Multiple EoA images per pick — how many filenames per row? (d) Is there a database dump format Austin also wants, or CSV only? | Austin/Dan       | Medium   | **Partially closed — format and columns confirmed; delivery path and multi-image handling still open (see OQ-24)**                                                                                                                                                                                                                                         |
| OQ-06 | ~~GxP / FDA 21 CFR Part 11 compliance~~                                                                                                                                                                                                                                                                                                                                                                                                                                                           | —                | —        | **REMOVED — not in scope for this system**                                                                                                                                                                                                                                                                                                                 |
| OQ-07 | Epson RC+ SDK Windows license availability on HMI, or use raw TCP socket?                                                                                                                                                                                                                                                                                                                                                                                                                         | SW               | Medium   | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-08 | M-Drive baud rate and exact command set — confirm with datasheet (if RS-232 path selected)                                                                                                                                                                                                                                                                                                                                                                                                        | Hardware team    | Low      | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-09 | ~~HMI OS — Windows or Ubuntu?~~                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | IT / HW          | High     | **CLOSED: Windows 11 confirmed (ICOP embedded computer)**                                                                                                                                                                                                                                                                                                  |
| OQ-10 | Source sorting dish geometry — what is the physical format of the sorting dish (the dish where specimens are loaded for picking)? How many specimen positions, what physical size? This affects the robot travel envelope and vision detection area.                                                                                                                                                                                                                                              | Austin / Lab     | Medium   | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-11 | Logic PLC Modbus register map — addresses for: status light output, work light output, hardware event log read. Safety state comes from the Sick safety controller, not this PLC.                                                                                                                                                                                                                                                                                                                 | David            | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-12 | ~~Safety controller status access path~~                                                                                                                                                                                                                                                                                                                                                                                                                                                          | David            | High     | **CLOSED (→ DTM-5): Python reads Sick Flexi controller directly via ModbusTCP. The Logic PLC is not in this path. pymodbus implementation must use endian-aware decoder — byte-order issue was encountered in a prior implementation.** |
| OQ-13 | Lighting controller model and Modbus register map for channel on/off control                                                                                                                                                                                                                                                                                                                                                                                                                      | Hardware team    | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-14 | M-Drive Smart Motor interface — Ethernet (ModbusTCP) or RS-232? Confirm actual interface to be used                                                                                                                                                                                                                                                                                                                                                                                               | David / Hardware | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-15 | Lighting trigger method — software via ModbusTCP, or hardwired I/O from robot/PLC? Decision affects software architecture                                                                                                                                                                                                                                                                                                                                                                         | SW + HW (David)  | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-16 | ~~Logic PLC model~~                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | David            | High     | **CLOSED: AutomationDirect P1-622 confirmed**                                                                                                                                                                                                                                                                                                              |
| OQ-17 | ~~Sample / plate / run identification~~                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Austin/Dan       | High     | **CLOSED: Field code (alphanumeric, e.g. `12345`) identifies the bulk sample/sorting dish. Plate ID (operator-entered label per station) identifies the destination plate. See Section 2 Glossary and FR-SETUP-01.**                                                                                                                                       |
| OQ-18 | ~~Plate layout and load order — single combined file or two separate files?~~                                                                                                                                                                                                                                                                                                                                                                                                                     | Tejas / Austin   | Medium   | **CLOSED: Only a plate config file is needed. There is no separate load order config. Well fill sequence is defined within the plate config.**                                                                                                                                                                                                             |
| OQ-19 | ~~Light curtain mode — protective stop vs. e-stop?~~                                                                                                                                                                                                                                                                                                                                                                                                                                              | David / Austin   | High     | **PARTIALLY CLOSED: Light curtain = latching protective stop. Hardware stops robot immediately. Latch stays until operator presses CONTINUE on HMI (CONTINUE button disabled until light curtain reads clear → DTM-10) → software writes reset-acknowledge to Sick controller → latch releases. No physical reset button required (unlike e-stop). Confirm Sick controller register for reset-acknowledge write — see OQ-23.** |
| OQ-20 | EoA imaging lighting — ring light only, or both back light and ring light (multiple images)? Affects cycle time and image analysis pipeline                                                                                                                                                                                                                                                                                                                                                       | Lab / SW         | Medium   | Open — see OQ-24                                                                                                                                                                                                                                                                                                                                           |
| OQ-21 | Vision algorithm selection — OpenCV (open source, classical contour/blob detection) vs. Halcon (MVTec commercial library, more powerful built-in tools)? Tejas to evaluate. Decision affects software dependencies and licensing cost.                                                                                                                                                                                                                                                            | Tejas            | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-22 | Config file format for plate configs — who creates these files, and how? Options: (a) **CSV** — operator creates in Excel, saves as .csv; (b) **Excel (.xlsx)** — operator works directly in Excel. A template file shall be provided. File placement onto the HMI is outside software scope — see FR-SETUP-09. *(→ DTM-3)* | Austin / Tejas   | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-23 | **Sick Flexi safety controller exact model + Modbus register map.** Required before implementing safety driver. Specifically need: (a) registers for reading light curtain state, e-stop states (individual buttons), contactor state, reset state, fault type; (b) register(s) for writing the protective-stop reset-acknowledge to release light curtain latch; (c) register(s) for writing the light curtain circuit reset after e-stop recovery. *(→ DTM-8, DTM-9)* | David            | High     | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-24 | **Multiple EoA images per pick** — Austin noted the lab may want more than one EoA image per specimen, especially early in the project. Confirm: (a) How many images per pick (one per lighting variant? One before pick, one after?); (b) Which lighting combinations (ring light only / backlight only / both); (c) How image filenames are represented in the CSV (one column? comma-separated within one column? multiple columns?); (d) Whether this changes before v1 ships.                | Austin / Tejas   | Medium   | Open                                                                                                                                                                                                                                                                                                                                                       |
| OQ-25 | **Barcode scanner for plate ID entry** — Austin's team is flexible on plate labeling. Confirm: (a) 1D vs. 2D barcode type; (b) Label location on plate (side label, short side, etc.); (c) Scanner interface (USB HID is most common and simplest — plug in and it emulates keyboard input); (d) Whether USB HID is acceptable (no driver needed) or a dedicated serial scanner is preferred. v1 uses manual text entry; barcode scanner planned for future release.                              | Austin / Tejas   | Low      | Open                                                                                                                                                                                                                                                                                                                                                       |

---

## 17. Revision History

| Version | Date       | Author | Changes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------- | ---------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.1     | 2026-05-28 | TBD    | Initial draft                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 0.2     | 2026-05-28 | TBD    | Updated from Controls_Network_Diagram.pptx: corrected PLC to AD P1-622, added separate safety controller (hardwired), added light curtain + e-stop + reset + contactor to safety circuit, confirmed Windows 11 OS, confirmed lighting on ModbusTCP, added two-switch network topology, added safety_events table, closed OQ-02 and OQ-09, added OQ-11/12/13                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 0.3     | 2026-06-03 | TBD    | Updated from David's network requirements doc: renamed safety controller to Pilz Flexi Compact (later corrected); corrected M-Drive role from linear stage to syringe pump motor; added pneumatic stage; identified HMI as ICOP embedded computer; added PLC data logger role; detailed main operating cycle (9-step); added pre-run setup requirements (FR-SETUP); updated UI button spec; added light curtain protective stop vs e-stop distinction; added e-stop indicator light behavior; noted ModbusTCP availability on safety controller; noted lighting hardwired trigger option; added OQ-14 through OQ-20; closed OQ-02 (updated); updated hardware table with ownership assignments                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 0.4     | 2026-06-03 | TBD    | Clarified pneumatic stage role (pipette deploy/retract via robot digital I/O); clarified safety contactor purpose; updated vision language to "vision algorithm (OpenCV or Halcon — OQ-21)"; added OQ-21; added Appendix A — full end-to-end communication flow                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 0.5     | 2026-06-05 | TBD    | Major update from Austin (CIB) input: (1) Added Section 2 Glossary. (2) Added Appendix B — CSV output format. (3) Corrected safety controller brand to Sick; updated interface to read + reset-acknowledge write. (4) Closed OQ-17, OQ-18, OQ-16. (5) Partially closed OQ-19. (6) Removed load_orders directory. (7) Rewrote picks table and CSV output. (8) Added FR-RUN-06/07. (9) Added OQ-23/24/25. (10) Added NFR-12. (11) Marked overhead images as debug-only. |
| 0.6     | 2026-06-08 | TBD    | Incorporated David (DTM) review comments (see Appendix C): (DTM-1) Mid-plate field code change redesigned as full-stop + new run flow — removed CHANGE FIELD CODE button/state; updated Goals, FR-SETUP-04, FR-UI-04, FR-RUN-06/07, state machine, Appendix A.13, Glossary Run definition. (DTM-2) Plate ID now required to activate a plate station — updated FR-SETUP-01/03, FR-UI-01, FR-RUN-02, Appendix A.2/A.3. (DTM-3) Config file transfer declared out of scope — replaced FR-SETUP-09/11, updated Appendix A.2, added to Non-Goals. (DTM-4) Config management screen shows filename list only, no preview/editing — updated FR-SETUP-10, FR-UI-12, Section 15, Non-Goals. (DTM-5) OQ-12 closed — Python reads Sick Flexi directly via ModbusTCP; endian-aware pymodbus decoder required — updated Section 7, Section 11.3, Section 10, OQ-12 table row. (DTM-6) FR-SAF-03 reworded to clarify software state is a mirror of hardware state. (DTM-7) FR-SAF-04 updated with note that hardware independently enforces run-start block. (DTM-8) FR-SAF-05 updated — software must also reset light curtain circuit after e-stop; OQ-23 updated to include this register. (DTM-9) FR-SAF-07 updated — fault detail data sourced from Flexi controller registers; Section 4, Section 8.4, FR-UI-06 updated. (DTM-10) FR-SAF-08/09, FR-UI-05, state machine table, Appendix A.10 updated — CONTINUE button disabled until light curtain physically clear per live poll. |

---

*This document will be updated frequently as hardware is confirmed and requirements are refined. All open questions in Section 16 must be resolved before development of the affected modules begins.*

---

## Appendix A — End-to-End Communication Flow

This appendix traces every communication that occurs from the moment an operator presses START through one complete pick-and-place cycle.

### A.1 Persistent Background Activity (always running once app starts)

| Loop                        | Thread               | What it does                                                                                                                        | Rate        |
| --------------------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| Sick Flexi safety controller poll | Safety worker thread | ModbusTCP read directly from Sick Flexi controller → checks e-stop states, light curtain state (latching), contactor state, reset button state, fault type registers *(→ DTM-5, DTM-9)* | Every 100ms |
| Logic PLC poll              | PLC worker thread    | ModbusTCP read/write → Logic PLC → writes status light and work light state; reads hardware event log                               | Every 100ms |
| Robot status poll           | Robot worker thread  | TCP read → Epson T3-B → checks robot ready/moving/error state                                                                       | Every 200ms |
| UI update                   | Qt main thread       | Receives state updates from asyncio via Qt signals, redraws UI; CONTINUE button enabled/disabled based on light_curtain_clear flag *(→ DTM-10)* | On event    |

The **Sick safety controller** runs entirely independently in hardware at all times — it does not wait for software.

---

### A.2 Pre-Run Setup (Before START)

```
[Operator] opens pre-run setup screen on touchscreen
  1. Enters field code for the current sorting dish (e.g. "12345")
  2. Enters Plate ID for each station they want to activate
     (Plate ID must be entered before a station can be activated — → DTM-2)
  3. Taps plate config dropdown → list populated from C:\LabAutomation\config\plate_configs\
     Selects desired plate config file
  4. Taps the station map to mark which plate stations have plates seated
     (Only stations with a Plate ID already entered can be activated — → DTM-2)
  5. (Optional) Specifies starting well per station
     (Used when continuing mid-plate after a field code change via abort+new run — → DTM-1)
  6. START button becomes active once all required fields are valid

[Config files on the HMI disk]
  How files are placed onto the HMI is outside the scope of this software.
  The software reads from C:\LabAutomation\config\plate_configs\ regardless of
  how files arrived there. (→ DTM-3)
```

### A.3 Operator Presses START

```
[Operator] touches START button on touchscreen
    │
    ▼
[PyQt6 UI — Qt main thread]
  Puts START_REQUESTED event onto thread-safe queue
    │
    ▼
[asyncio event loop — state machine]
  Reads event from queue
  Checks pre-conditions:
    → Safety poll: Sick Flexi controller reports no active fault?
    → Robot worker: robot connected and ready?
    → M-Drive worker: pump connected?
    → Camera threads: cameras connected?
    → Logic PLC: connected?
    → Config: plate config loaded and validated?
    → Run info: field code entered? Plate IDs entered for ALL active stations? (→ DTM-2)
    → Stations: at least one station marked active (each with a Plate ID)?
  If all clear:
    → Transitions state: IDLE → RUNNING
    → Sends RUNNING state update → Qt signal → UI redraws (shows CYCLE STOP button)
    → Sends SET_STATUS_LIGHT=GREEN → PLC worker → ModbusTCP write → Logic PLC
```

---

### A.4 Step 1 — Overhead Image Acquisition

```
[asyncio state machine]
  Sends BACKLIGHT_ON → lighting worker thread
    │
[Lighting worker thread]
  ModbusTCP write → Lighting Controller
  Backlight turns ON physically
    │
[asyncio state machine]
  Waits configured lead time (e.g. 50ms)
  Sends CAPTURE_OVERHEAD → overhead camera worker thread
    │
[Overhead camera worker thread]
  GigE software trigger → Overhead Camera (POE switch)
  Frame captured and streamed back → numpy array
  Saves raw TIFF → runs\{run_id}\overhead\frame_NNN.tiff  (debugging only)
  Puts IMAGE_READY + numpy array onto asyncio queue
    │
[asyncio state machine]
  Sends BACKLIGHT_OFF → lighting worker → backlight off
```

---

### A.5 Step 2 — Vision Analysis on HMI

```
[asyncio state machine]
  Dispatches numpy array to vision thread pool worker (run_in_executor)
    │
[Vision worker — HMI CPU, no network I/O]
  Gaussian blur → background subtraction → threshold → morphological operations
  → Contour detection → filter by area → compute pixel centroids
  → Apply calibration homography → robot XY coordinates
  Puts CENTROIDS_READY + [list of robot XY] onto asyncio queue
    │
[asyncio state machine]
  Selects next target specimen from centroid list (first unvisited)
  If zero specimens detected → sends ALERT to UI, waits for operator
```

---

### A.6 Steps 3–4 — Robot Moves to Pick Site, EoA Image Captured

```
[asyncio state machine]
  Sends MISSION_IMAGE(target_x, target_y) → robot worker thread
    │
[Robot worker thread]
  TCP command → Epson T3-B: "move to [target_x, target_y, safe_Z]"
  Robot executes move; worker polls status via TCP every 200ms
  Robot reaches position → puts ROBOT_IN_POSITION onto asyncio queue
    │
[asyncio state machine]
  Triggers EoA camera capture (software or hardware trigger — OQ-03)
  EoA worker receives frame(s), saves TIFF(s) → runs\{run_id}\eoa\eoa_NNN_a.tiff, eoa_NNN_b.tiff, ...
  Puts EOA_IMAGE_READY onto asyncio queue
```

---

### A.7 Steps 5–6 — EoA Vision Analysis and Go/No-Go Decision

```
[asyncio state machine]
  Dispatches EoA frame to vision thread pool worker
    │
[Vision worker]
  Analyzes close-up image → is there a pickable specimen? → refined XY coordinates?
  Puts VISION_RESULT(go=True/False, refined_x, refined_y) onto asyncio queue
    │
[asyncio state machine]
  If go=False → ROBOT_MOVE_CLEAR → return to Step 1 (or try alternate coords first)
  If go=True → continues to Step 7
```

---

### A.8 Steps 7–8 — Pick Execution

```
[asyncio state machine]
  Records timestamp_start for this pick
  Looks up next well position from plate config (in-memory) for the active plate station
  Sends MISSION_PICK_PLACE(pick_x, pick_y, well_x, well_y) → robot worker thread
    │
[Robot worker thread → Epson T3-B]

  a. Robot moves to safe height above pick site

  b. Robot fires digital output → pneumatic valve opens → pipette deploys

  c. Robot moves to refined pick XY position

  d. ROBOT_AT_PICK_POSITION received
     → asyncio sends ASPIRATE → M-Drive worker
     → M-Drive aspirates specimen (syringe pulls back)
     → ASPIRATE_COMPLETE put onto queue

  e. [Optional pick confirmation — OQ-04]

  f. Robot moves to safe height, then toward target plate station

  g. *** AS ROBOT EXITS OVERHEAD CAMERA FIELD OF VIEW ***
     Triggers next overhead image acquisition in parallel (overlaps robot travel time)

  h. Robot descends above target well

  i. ROBOT_AT_PLACE_POSITION received
     → asyncio sends DISPENSE → M-Drive worker
     → M-Drive dispenses specimen into well (syringe pushes forward)
     → DISPENSE_COMPLETE put onto queue

  j. Robot fires digital output → pneumatic valve closes → pipette retracts

  k. MISSION_COMPLETE received from robot via TCP
```

---

### A.9 Step 9 — Log Pick and Return to Step 2

```
[asyncio state machine]
  Records timestamp_end for this pick
  Writes pick to SQLite database (picks table):
    field_code, plate_id, plate_position, well_position, robot_x, robot_y,
    timestamp_start, timestamp_end, result, error_code, error_message, image_filenames
  Appends one row to the run CSV file (incremental write — NFR-12)
  Sends UI update: pick count, pass/fail count, elapsed time (via Qt signal)
  Uses overhead image already acquired during step 8g — no new capture needed
  Returns to Step 2 (vision analysis on that image)
  Cycle repeats until: all specimens picked, CYCLE STOP pressed, or fault occurs
```

---

### A.10 Safety Monitoring — Runs Concurrently at All Times

```
━━━ LOOP 1: Sick Flexi safety controller poll (safety worker thread — every 100ms) ━━━
    (Python reads directly from Sick Flexi controller — OQ-12 resolved, → DTM-5)

  ModbusTCP read → Sick Flexi controller (main switch)
  Returns: {light_curtain_state, latched, estop_states[], contactor_state, reset_state, fault_type}
  Puts SAFETY_STATUS event onto asyncio queue
    │
  [asyncio state machine evaluates every SAFETY_STATUS event]
  [Also: light_curtain_clear flag propagated to UI thread via Qt signal
   to enable/disable CONTINUE button in real time — → DTM-10]

  SCENARIO A — Light curtain tripped (latching protective stop):
    *** HARDWARE FIRST (independent of software, <10ms) ***
    Sick controller instantly removes robot safety signal → robot stops in hardware
    Sick controller opens safety contactor → M-Drive loses power
    *** LIGHT CURTAIN LATCH ENGAGED ***
    Circuit stays open even after person steps back out — does NOT auto-reset
    *** SOFTWARE FOLLOWS (next 100ms poll cycle) ***
    Safety poll detects light_curtain_state = TRIPPED, latched = TRUE
    → State machine: RUNNING → PROTECTIVE_STOP
    → Logic PLC: SET_STATUS_LIGHT=AMBER → ModbusTCP write → amber light
    → UI: shows PROTECTIVE STOP banner, "Light curtain tripped — zone clear, then press CONTINUE"
    → ABORT button active immediately
    → CONTINUE button DISABLED until light curtain_clear = TRUE (→ DTM-10)

    [100ms poll continues]
    When poll detects light_curtain_state = CLEAR:
    → light_curtain_clear flag set TRUE → Qt signal → CONTINUE button becomes ENABLED

    Operator verifies zone is clear, presses CONTINUE on HMI (button was grayed out until now):
    → Software confirms light_curtain_clear = TRUE before sending reset
    → Sends reset-acknowledge WRITE → Sick Flexi controller via ModbusTCP
      Sick controller releases latch → circuit closes → contactor closes → robot ready
      → State machine: PROTECTIVE_STOP → RUNNING
      → Logic PLC: SET_STATUS_LIGHT=GREEN
      → Cycle resumes

    The machine NEVER resumes automatically when the person leaves the zone.
    Operator CONTINUE → software write → is always required.

  SCENARIO B — E-stop button pressed:
    *** HARDWARE FIRST (independent of software, <10ms) ***
    Sick controller removes robot safety signal → robot stops
    Safety contactor opens → M-Drive loses power
    Pressed e-stop button light FLASHING; other e-stop button lights LIT STEADY
    *** SOFTWARE FOLLOWS (next 100ms poll) ***
    Safety poll detects: estop_active = TRUE, contactor_state = OPEN
    Fault type read from Flexi controller registers (which button pressed — → DTM-9)
    → State machine: RUNNING → E_STOP
    → Logic PLC: SET_STATUS_LIGHT=RED → red light
    → UI: large red E-STOP alert showing which button was pressed (from Flexi registers)
    → All motion commands blocked in software

    Hardware reset sequence (must be done physically):
      1. Operator physically releases the e-stop button
         → Button stops flashing; blue reset button starts FLASHING
      2. Operator presses and releases blue reset button
         → Sick controller returns safety circuit to operational mode
         → Contactor closes → M-Drive regains power → robot safety signal restored
         → Blue reset light stops flashing
    Safety poll detects: estop_active = FALSE, contactor_state = CLOSED
    → Software additionally sends light curtain circuit reset-acknowledge to Sick controller
      (register TBD — OQ-23, → DTM-8)
    → Software enables CONTINUE + ABORT in UI

    Operator presses CONTINUE → State machine: E_STOP → IDLE (re-home, re-detect)

━━━ LOOP 2: Logic PLC poll (PLC worker thread — every 100ms) ━━━

  ModbusTCP read/write → Logic PLC
  Python writes: current system state → status light colour (green/amber/red)
  Python reads: hardware event log entries → appended to structured application log
  (The Logic PLC does NOT carry safety state — that comes from the Sick Flexi controller above)

━━━ SCENARIO C — Connection loss ━━━

  Any worker thread detects timeout / connection error
  → Puts CONNECTION_LOST(device_name) onto asyncio queue
  → State machine: RUNNING → FAULT
  → UI: shows FAULT with device name
  → Operator must investigate and reconnect before restarting
```

---

### A.11 Logic PLC's Specific Role

The Logic PLC is **not a safety device**. It does exactly two things:

1. **Output controller:** Controls the cell's status indicator light (tower light) and work light. Python writes the desired colour state via ModbusTCP; the PLC drives the physical outputs.
2. **Hardware event logger:** Records hardware-level events at the PLC level, independent of the Python structured log. Useful for debugging hardware issues.

The Logic PLC does **not** carry safety state. E-stop status, light curtain state, and contactor state come from the Sick Flexi safety controller (Section A.10, Loop 1), not from this device.

---

### A.12 What the Ewon Cosy+ Does (Not Involved During Normal Operation)

The Ewon Cosy+ is **not involved in any normal operating cycle communication**. Its role is:

- Acts as a hardware firewall — isolates the cell's internal Ethernet network from the WAN
- Provides a VPN endpoint — KTM engineers connect to it remotely and their laptop appears as if physically plugged into the main switch
- Used for remote programming, troubleshooting, and software updates without being on-site

During normal operation, all traffic on the main switch is purely internal (HMI ↔ Robot, HMI ↔ PLC, etc.).

---

### A.13 Mid-Plate Field Code Change Flow

This appendix section traces the communication when an operator changes the field code mid-plate. The field code change is handled as an abort-and-new-run workflow — each field code segment is a separate run. *(→ DTM-1)*

```
[Operator] presses CYCLE STOP during a run
    → State machine: RUNNING → CYCLE_STOPPING (finishes current pick cycle)
    → State machine: CYCLE_STOPPING → IDLE (robot parks)
    → Logic PLC: SET_STATUS_LIGHT=AMBER
    → UI: shows CONTINUE and ABORT buttons

[Operator] removes old sorting dish, loads new sorting dish, presses ABORT
    → Current run ends; run data saved (CSV written, SQLite updated with status=aborted)
    → State machine: IDLE → IDLE (pre-run setup screen shown)

[Operator] on pre-run setup screen:
    1. Enters new field code
    2. Enters Plate IDs for each station (same plates, or new ones)
       (Must enter Plate ID before activating each station — → DTM-2)
    3. Activates stations on the station map
    4. Specifies starting well per station (to continue from where previous run left off)
    5. Presses START

[asyncio state machine]
    → Validates all pre-run conditions
    → Transitions: IDLE → RUNNING
    → New run begins with new run_id, new field code, from specified starting wells
    → All CSV rows and database picks tagged with the new field code and new run_id
    → Status light: GREEN
```

---

## Appendix B — CSV Output Format

This is the primary lab-facing output. One file per run. Rows are appended incrementally as picks complete (see NFR-12). The file shall be importable directly into Austin's LIMS database.

**File naming:** `{run_id}_picks.csv` (e.g. `20260605_142250_picks.csv`)

**Column specification:**

| Column            | Type              | Description                                                                                               | Example                                           |
| ----------------- | ----------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `field_code`      | string            | Field collection identifier for this specimen                                                             | `12345`                                           |
| `plate_id`        | string            | Operator-entered plate label for the destination plate                                                    | `CIB-2026-001`                                    |
| `plate_position`  | integer           | Which plate station (1–4)                                                                                 | `2`                                               |
| `well_position`   | string            | Human-readable well identifier from plate config                                                          | `A1`                                              |
| `image_filename`  | string            | Filename(s) of EoA archival TIFF image(s) for this pick. If multiple images per pick, format TBD — OQ-24. | `eoa_042_a.tiff`                                  |
| `timestamp_start` | ISO 8601 datetime | When this pick cycle began                                                                                | `2026-06-05T14:23:01.123Z`                        |
| `timestamp_end`   | ISO 8601 datetime | When this pick cycle completed                                                                            | `2026-06-05T14:23:05.841Z`                        |
| `result`          | string            | Outcome of this pick                                                                                      | `success` / `failed_pick` / `skipped` / `aborted` |
| `error_code`      | string            | Machine-readable error identifier (empty if success)                                                      | `PICK_TIMEOUT`                                    |
| `error_message`   | string            | Human-readable error detail (empty if success)                                                            | `Aspirate timeout after 3s`                       |

**Notes:**

- `image_filename` format when multiple images per pick (OQ-24): options are (a) comma-separated values within the column, (b) multiple columns (`image_filename_1`, `image_filename_2`), or (c) semicolon-separated. To be confirmed with Austin.
- Images are stored in `C:\LabAutomation\data\runs\{run_id}\eoa\` — the CSV column contains filename only (not full path), with the run ID implied.
- `result` values: `success` (specimen picked and placed), `failed_pick` (aspirate attempted but specimen not confirmed picked), `skipped` (vision said no specimen at this location), `aborted` (operator aborted mid-pick).
- JPEG images are not acceptable. TIFF is required; PNG is acceptable if TIFF is blocked.

---

## Appendix C — David (DTM) Review Comments

The following are David's original review comments verbatim, as they appeared in prd.md v0.5. Each comment has been resolved and incorporated into this document (v0.6). Section references point to where changes were applied.

---

**DTM-1** *(appeared at: Section 3 Goals, line 74)*

> "Do we want to allow this from a cycle stop, or make them fully stop the run and re-start it with new parameters? They could do this and then select the start location mid-plate where they want to resume."

**Resolution:** Adopted David's suggestion — Option B (full stop + new run). Mid-plate field code changes now require the operator to CYCLE STOP → ABORT → start a new run with the new field code and starting well on the pre-run setup screen. There is no "CHANGE FIELD CODE" button or mid-run state. Each field code segment is a separate run with its own run ID and CSV file.

**Applied at:** Section 3 Goals, Section 2 Glossary (Run definition), FR-SETUP-04, FR-UI-04, FR-UI-14 (removed), FR-RUN-06, FR-RUN-07, Section 12 State Machine, Section 14.1 Data Directory Notes, Appendix A.2, Appendix A.13.

---

**DTM-2** *(appeared at: Section 8.1 FR-SETUP-03, line 232)*

> "Do we want to require a Plate ID to be entered in order to enable a plate station? We can ask if there is ever a time they would load plates without a plate ID, and what the logged data would include if we did that."

**Resolution:** Yes — Plate ID is required to activate a station. A station cannot be marked active without a Plate ID entered for it. This prevents logging entries with blank plate IDs, which would be unusable for Austin's LIMS.

**Applied at:** FR-SETUP-01, FR-SETUP-03, FR-UI-01 (station map behavior), FR-RUN-02, Appendix A.2, Appendix A.3.

---

**DTM-3** *(appeared at: Section 8.1 FR-SETUP-09, line 248)*

> "In reality they could do this any way they can get a file onto the computer. I guess Windows does not have SCP, but any other way would work. I also think this process is outside of the software we are writing. The software can just take for granted the config files will be in the directory and it's outside the scope of the software how they got there."

**Resolution:** Agreed — file transfer to the HMI is outside software scope. The software reads from `C:\LabAutomation\config\plate_configs\` and does not manage how files arrive there.

**Applied at:** FR-SETUP-09 (rewritten), FR-SETUP-11 (removed), Section 3 Non-Goals (added), OQ-22 (updated note), Appendix A.2.

---

**DTM-4** *(appeared at: Section 8.1 FR-SETUP-10, line 250)*

> "How would we display them, just open them in Notepad++ or something?"

**Resolution:** The config management screen shows a list of filenames only. Users can select and delete files. There is no in-app preview or editing of file contents — config files are created and modified externally on a laptop.

**Applied at:** FR-SETUP-10 (rewritten), FR-UI-12 (updated), Section 3 Non-Goals (added), Section 15.

---

**DTM-5** *(appeared at: Section 8.4 OQ-12 note, line 281)*

> "We can do it either way. The easiest for me is to read it straight from the Flexi controller. Rick had some issues with this and whatever library he was using. I think it was a big endian little endian issue that he could not change in his library."

**Resolution:** OQ-12 closed. Python reads directly from the Sick Flexi controller via ModbusTCP. The `pymodbus` implementation must use an endian-aware decoder. The "Flexi controller" is the working name for the Sick safety controller throughout this document.

**Applied at:** OQ-12 (closed), Section 7 Software Stack (safety controller comms row), Section 11.3 (implementation note added), Section 5 Network Topology, Section 10 Concurrency Model, Appendix A.1, Appendix A.10.

---

**DTM-6** *(appeared at: Section 8.4 FR-SAF-03, line 289)*

> "This would be a software state only, the software does not control the hardware going into this state, that's all done at a hardware level."

**Resolution:** FR-SAF-03 reworded to explicitly state that the E_STOP/PROTECTIVE_STOP state machine transition is a software mirror of the hardware state — the hardware has already acted independently before the software detects and mirrors it.

**Applied at:** FR-SAF-03.

---

**DTM-7** *(appeared at: Section 8.4 FR-SAF-04, line 290)*

> "This is correct, but the hardware enforces this. Even if the software tried to start a run, if the safety system is not happy, the hardware won't actually do anything."

**Resolution:** FR-SAF-04 updated to note that the software check is a UX gate providing operator feedback; the hardware independently enforces the same constraint regardless of software state.

**Applied at:** FR-SAF-04.

---

**DTM-8** *(appeared at: Section 8.4 FR-SAF-05, line 291)*

> "The software controller will likely need to also reset the light curtain circuit too."

**Resolution:** FR-SAF-05 updated — after the e-stop hardware reset sequence completes, software must also send a light curtain circuit reset-acknowledge to the Sick controller. The specific register is pending OQ-23. OQ-23 updated to explicitly request this register.

**Applied at:** FR-SAF-05, Section 11.3, OQ-23, Appendix A.10 Scenario B.

---

**DTM-9** *(appeared at: Section 8.4 FR-SAF-07, line 293)*

> "This information will be pulled from the Flexi controller."

**Resolution:** FR-SAF-07 updated to explicitly state that specific fault type information (which e-stop button, light curtain vs. contactor) is sourced from the Sick Flexi controller's Modbus registers, pending OQ-23 register map.

**Applied at:** FR-SAF-07, Section 4 Hardware Inventory, Section 8.4 intro blockquote, FR-UI-06, Appendix A.10.

---

**DTM-10** *(appeared at: Section 8.4 FR-SAF-08, line 298)*

> "We might want to monitor the light curtain status and only enable continue if the light curtain is clear and the system is actually resetable. Otherwise they may press the button and the system can't be reset, for example if they still have their arm in the cell breaking the light curtain."

**Resolution:** Adopted. The CONTINUE button in PROTECTIVE_STOP state is disabled (grayed out) and only becomes enabled when the 100ms safety poll confirms the light curtain is physically clear. The button state is driven by the live `light_curtain_clear` flag propagated from the safety poll to the UI via Qt signal.

**Applied at:** FR-SAF-08 (CONTINUE button behavior), FR-SAF-09 (reinforced), FR-UI-05 (CONTINUE disabled until clear), Section 12 State Machine table (PROTECTIVE_STOP row), Section 10 Concurrency Model, Appendix A.1, Appendix A.10 Scenario A.
