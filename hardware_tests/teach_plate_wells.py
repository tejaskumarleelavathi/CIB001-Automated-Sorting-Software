"""
teach_plate_wells.py — teach 3 reference wells, compute the full 96-well
grid, and write a plate config Excel file in the exact format the software
already loads (config/create_plate_template.py's column structure).

This is the TEST-SCOPED tool discussed 2026-08-11 (see prd_updated.md
Appendix D) — quick way to get real well coordinates for the plate
currently installed, without manually jogging+typing all 96 wells by hand.
NOT the final production design (that needs vision-coverage /
nest-repeatability questions resolved first — see Appendix D).

METHOD
  Teach 3 wells, not 1 — A1 (origin), A12 (defines the column direction +
  spacing), H1 (defines the row direction + spacing). Using 3 measured
  points instead of assuming perfect perpendicularity/pitch means any real
  fixture rotation or spacing deviation from nominal 9mm gets captured, not
  assumed away.

  Positioning is done by the SCRIPT (via MOVE_PIPETTE_U, Tool 2, no
  actuation), nudged with keyboard input — NOT by manually jogging on the
  teach pendant and typing in what it shows. This sidesteps any ambiguity
  about which tool frame the pendant display is currently showing: the
  script always knows the exact position it commanded, because it sent it.

CONTROLS (nudge window)
  Arrow keys or WASD — nudge X/Y by the current step size
                       (W/Up=Y-, S/Down=Y+, A/Left=X-, D/Right=X+)
  + / -        — double / halve step size
  I            — set step size to 5.0mm directly
  K            — set step size to 1.0mm directly
  P            — extend pipette DOWN at the current XY (manual, no aspirate)
  U            — retract pipette back UP
  ENTER        — confirm this well's position, move to the next
                 (auto-retracts pipette first if it was left down)
  Q / Esc      — abort (auto-retracts pipette first if it was left down)

  Pipette always starts UP — it only extends when you explicitly press P,
  and any XY move (including moving to the next well) forces it back UP
  first, so it's never dragged sideways.

USAGE
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/teach_plate_wells.py
"""

import os
import socket
import sys

import cv2
import numpy as np
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
ROBOT_TIMEOUT = 30.0

ROWS = 8    # A-H
COLS = 12   # 1-12
STEP_DEFAULT = 1.0   # mm

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "plate_configs", "test_96well.xlsx")


# ── Robot ─────────────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket:
    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ROBOT_TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] {exc}")
        print("  Make sure sort_server.prg is running in RC+ (F5).")
        sys.exit(1)
    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected response: {resp!r}")
        sys.exit(1)
    print("  Robot ready.")
    return s


def _recv_line(s: socket.socket) -> str:
    buf = b""
    while True:
        ch = s.recv(1)
        if not ch:
            raise ConnectionError("Robot disconnected.")
        buf += ch
        if ch == b"\n":
            break
    return buf.decode("ascii", errors="replace").strip()


def send_cmd(s: socket.socket, cmd: str) -> str:
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def move_pipette(s: socket.socket, rx: float, ry: float, u: float | None) -> None:
    cmd = (
        f"MOVE_PIPETTE_U {rx:.3f},{ry:.3f},{u:.3f}"
        if u is not None
        else f"MOVE_PIPETTE {rx:.3f},{ry:.3f}"
    )
    resp = send_cmd(s, cmd)
    if not resp.startswith("OK PIPETTE_POSITION"):
        raise RuntimeError(f"{cmd.split()[0]} failed: {resp!r}")


def pipette_down(s: socket.socket) -> None:
    resp = send_cmd(s, "PIPETTE_DOWN")
    if not resp.startswith("OK PIPETTE_DOWN"):
        raise RuntimeError(f"PIPETTE_DOWN failed: {resp!r}")


def pipette_up(s: socket.socket) -> None:
    resp = send_cmd(s, "PIPETTE_UP")
    if not resp.startswith("OK PIPETTE_UP"):
        raise RuntimeError(f"PIPETTE_UP failed: {resp!r}")


def robot_home(s: socket.socket) -> None:
    print("  Homing robot ...")
    try:
        send_cmd(s, "HOME")
        print("  Homed.")
    except Exception as exc:
        print(f"  [WARN] HOME failed: {exc}")


# ── Nudge UI ──────────────────────────────────────────────────────────────────

def nudge_to_well(sock: socket.socket, label: str, start_x: float, start_y: float,
                  u: float | None) -> tuple[float, float]:
    """Interactive nudge session. Returns the confirmed (x, y)."""
    x, y = start_x, start_y
    step = STEP_DEFAULT
    down = False   # pipette state — always starts UP (MOVE_PIPETTE/_U forces retract first)
    move_pipette(sock, x, y, u)

    win = f"Teach {label} — arrows/WASD=nudge, P/U=pipette, ENTER=confirm, Q=quit"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 700, 290)

    while True:
        img = np.zeros((290, 700, 3), dtype=np.uint8)
        cv2.putText(img, f"Teaching well: {label}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        u_disp = f"{u:.1f}" if u is not None else "current (unspecified)"
        cv2.putText(img, f"Position: ({x:.3f}, {y:.3f}) mm  U={u_disp}", (20, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(img, f"Step: {step:.2f} mm", (20, 130),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)
        pip_colour = (0, 0, 255) if down else (0, 200, 0)
        cv2.putText(img, f"Pipette: {'DOWN' if down else 'UP'}", (300, 130),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, pip_colour, 2)
        cv2.putText(img, "Arrows/WASD=nudge (W/S=Y, A/D=X)  +/-=step  I=5mm K=1mm", (20, 175),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(img, "P=pipette down  U=pipette up  ENTER=confirm  Q=abort", (20, 200),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(img, "Look at the physical pipette tip vs the well opening.", (20, 235),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 255), 1)
        cv2.imshow(win, img)
        key = cv2.waitKeyEx(0)

        moved = False
        if key in (27, ord('q'), ord('Q')):
            if down:
                pipette_up(sock)
            cv2.destroyWindow(win)
            print("  Aborted.")
            sys.exit(1)
        elif key == 13:  # ENTER
            if down:
                pipette_up(sock)
            cv2.destroyWindow(win)
            print(f"  Confirmed {label}: ({x:.3f}, {y:.3f}) mm")
            return x, y
        elif key in (ord('p'), ord('P')):
            if not down:
                pipette_down(sock)
                down = True
        elif key in (ord('u'), ord('U')):
            if down:
                pipette_up(sock)
                down = False
        elif key in (ord('+'), ord('=')):
            step = min(step * 2, 10.0)
        elif key in (ord('-'), ord('_')):
            step = max(step / 2, 0.05)
        elif key in (ord('i'), ord('I')):
            step = 5.0
        elif key in (ord('k'), ord('K')):
            step = 1.0
        # Y axis: Up/W = -Y, Down/S = +Y
        elif key in (2490368, ord('w'), ord('W')):
            y -= step; moved = True
        elif key in (2621440, ord('s'), ord('S')):
            y += step; moved = True
        # X axis: Left/A = -X, Right/D = +X
        elif key in (2424832, ord('a'), ord('A')):
            x -= step; moved = True
        elif key in (2555904, ord('d'), ord('D')):
            x += step; moved = True

        if moved:
            move_pipette(sock, x, y, u)   # MOVE_PIPETTE_U retracts first regardless
            down = False


# ── Grid computation ─────────────────────────────────────────────────────────

def compute_grid(a1: tuple, a12: tuple, h1: tuple) -> dict:
    """Compute all ROWS x COLS well positions from 3 measured reference wells.

    col_vector = (A12 - A1) / (COLS - 1)   -- per-column step, includes any
                                               real rotation/spacing deviation
    row_vector = (H1  - A1) / (ROWS - 1)   -- per-row step, same

    well(r, c) = A1 + c*col_vector + r*row_vector
    """
    a1 = np.array(a1, dtype=float)
    a12 = np.array(a12, dtype=float)
    h1 = np.array(h1, dtype=float)

    col_vec = (a12 - a1) / (COLS - 1)
    row_vec = (h1 - a1) / (ROWS - 1)

    print(f"\n  Column vector (per well, A->12 direction): {col_vec} mm  "
          f"|v|={np.linalg.norm(col_vec):.3f}mm")
    print(f"  Row vector    (per well, A->H direction):  {row_vec} mm  "
          f"|v|={np.linalg.norm(row_vec):.3f}mm")
    print("  (nominal 96-well pitch is 9.00mm — compare against the above)")

    wells = {}
    for r in range(ROWS):
        for c in range(COLS):
            pos = a1 + c * col_vec + r * row_vec
            label = f"{chr(ord('A') + r)}{c + 1}"
            wells[label] = (float(pos[0]), float(pos[1]))
    return wells


# ── Excel writer (matches config/create_plate_template.py format) ────────────

def write_excel(wells: dict, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "plate_config"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    thin = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    headers = ["well", "x", "y", "aspirate_volume_ul", "aspirate_speed_ul_per_s",
               "dispense_volume_ul", "dispense_speed_ul_per_s"]
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    # Row order = fill order. Standard serpentine-free row-major: A1..A12, B1..B12, ...
    row_idx = 2
    for r in range(ROWS):
        for c in range(COLS):
            label = f"{chr(ord('A') + r)}{c + 1}"
            x, y = wells[label]
            ws.cell(row=row_idx, column=1, value=label).border = border
            ws.cell(row=row_idx, column=2, value=round(x, 3)).border = border
            ws.cell(row=row_idx, column=3, value=round(y, 3)).border = border
            for col in (4, 5, 6, 7):
                ws.cell(row=row_idx, column=col).border = border
            row_idx += 1

    col_widths = [10, 12, 12, 20, 22, 20, 22]
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    wb.save(out_path)
    print(f"\n  Saved: {out_path}  ({len(wells)} wells)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 70)
    print("  Teach Plate Wells — 3-point grid teach for a 96-well plate")
    print("=" * 70)
    print("""
  Teaches A1, A12, H1 (3 reference wells), computes the full 96-well grid,
  writes it to a plate config Excel file in the format the software loads.

  Uses MOVE_PIPETTE_U (Tool 2, no actuation) — the pipette will move and
  hover at pick/drop height, but will NOT extend or aspirate/dispense.

  CHECKLIST:
    - sort_server.prg running in RC+ (F5)
    - Plate physically seated in the station you're teaching
""")

    u_str = input("  U angle for this plate station (deg, blank = use current U): ").strip()
    if u_str:
        try:
            u = float(u_str)
        except ValueError:
            print("  Invalid U.")
            sys.exit(1)
    else:
        u = None

    x_str = input("  Rough starting X guess (mm): ").strip()
    y_str = input("  Rough starting Y guess (mm): ").strip()
    try:
        start_x, start_y = float(x_str), float(y_str)
    except ValueError:
        print("  Invalid X/Y.")
        sys.exit(1)

    sock = robot_connect()

    a1 = nudge_to_well(sock, "A1", start_x, start_y, u)
    a12 = nudge_to_well(sock, "A12", a1[0], a1[1], u)
    h1 = nudge_to_well(sock, "H1", a1[0], a1[1], u)

    wells = compute_grid(a1, a12, h1)

    print("\n  Computed wells (first row):")
    for c in range(COLS):
        label = f"A{c + 1}"
        wx, wy = wells[label]
        print(f"    {label:<4} ({wx:.2f}, {wy:.2f}) mm")

    write_excel(wells, OUT_PATH)

    if u is None:
        print(f"\n  PLATE_U = None (held whatever U the arm was already at)")
        print(f"  main.py's PLATE_U already defaults to None — no change needed there.")
    else:
        print(f"\n  PLATE_U = {u}")
        print(f"  Set this in main.py's PLATE_U constant — it is NOT stored in")
        print(f"  the Excel file (plate stations use one fixed U, not per-well).")

    robot_home(sock)
    try:
        send_cmd(sock, "BYE")
    except Exception:
        pass
    sock.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
