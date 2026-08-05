"""
step2_full_calibration.py  —  Combined Overhead Homography + EoA Tool Offset
=============================================================================

Runs two calibration phases in one session:

  PHASE 1 — Overhead homography  (same as step2_robot_grid_scan.py)
    You jog the EoA crosshair to checkerboard corner C2, confirm, then the
    robot auto-visits the 4 surrounding corners and computes H.

  PHASE 2 — EoA camera tool offset  (NEW)
    The EoA ring light turns on. A live feed window opens. The robot stays at
    C2 with U from Phase 1 (first measurement pre-recorded automatically).
    You then jog to a DIFFERENT U angle on the teach pendant, re-centre the
    EoA crosshair on C2, and press SPACE to record. Repeat for more U angles.
    Press D when done. The script solves for the camera's fixed (cx, cy) offset
    from the U rotation axis.

    The resulting SPEL+ Tool command is printed at the end — give it to David
    to enter in RC+ Setup → Robots → Robot 1 → Configuration.

OUTPUTS
  camera_calibration/output/calibration_overhead.npy  — 3×3 homography H
  camera_calibration/output/calibration_pairs.json    — overhead pairs + RMSE
  camera_calibration/output/tool_offset.json          — tool offset + SPEL+ command
  C:\\LabAutomation\\config\\calibration_overhead.npy  — runtime copy of H
  C:\\LabAutomation\\config\\tool_offset.json          — runtime copy of tool offset

REQUIRES
  calibration_v2_server.prg  loaded and running in RC+ (F5).
    (NOT calibration_server.prg — use the _v2 version)
  step1_detect_corners.py  already run  (output/corners.json must exist).
  EoA camera reachable (run force_camera_ip.py if needed).

USAGE
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step2_full_calibration.py
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import shutil
import socket
import sys
import time

import cv2
import numpy as np

# ── Paths ──────────────────────────────────────────────────────────────────────
OUT_DIR      = os.path.join(os.path.dirname(__file__), "output")
CORNERS_JSON = os.path.join(OUT_DIR, "corners.json")
H_LOCAL      = os.path.join(OUT_DIR, "calibration_overhead.npy")
H_RUNTIME    = r"C:\LabAutomation\config\calibration_overhead.npy"
TOL_LOCAL    = os.path.join(OUT_DIR, "tool_offset.json")
TOL_RUNTIME  = r"C:\LabAutomation\config\tool_offset.json"

# ── Robot ──────────────────────────────────────────────────────────────────────
ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 20.0   # seconds — move commands need time at 10% speed

# ── EoA camera ────────────────────────────────────────────────────────────────
EOA_SERIAL   = "253102169"
EOA_EXPOSURE = 60000   # µs — brighter than pipeline default, easier to see corners
DISPLAY_W    = 1280    # display width for live feed window

# ── Checkerboard grid (must match step1_detect_corners.py) ────────────────────
IDX = {
    "TL": 1,    # top-left   (col=1, row=0)
    "TR": 5,    # top-right  (col=5, row=0)
    "C2": 15,   # centre     (col=3, row=2)  ← reference corner
    "BL": 25,   # bot-left   (col=1, row=4)
    "BR": 29,   # bot-right  (col=5, row=4)
}
OFFSETS_FROM_C2 = {   # robot XY offsets (mm) from C2 to each other corner
    "TR": (-40, +40),
    "BR": (+40, +40),
    "TL": (-40, -40),
    "BL": (+40, -40),
}
VISIT_ORDER = ["TR", "BR", "TL", "BL"]


# ══════════════════════════════════════════════════════════════════════════════
# Robot TCP helpers
# ══════════════════════════════════════════════════════════════════════════════

def robot_connect() -> socket.socket:
    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] Cannot connect: {exc}")
        print("  Check: robot on, calibration_v2_server.prg running (F5).")
        sys.exit(1)
    return s


def _recv_line(s: socket.socket) -> str:
    buf = b""
    while True:
        ch = s.recv(1)
        if not ch:
            raise ConnectionError("Robot disconnected mid-response.")
        buf += ch
        if ch == b"\n":
            break
    return buf.decode("ascii", errors="replace").strip()


def send_cmd(s: socket.socket, cmd: str) -> str:
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def robot_where(s: socket.socket) -> tuple[float, float, float, float]:
    """Returns (x_mm, y_mm, z_mm, u_deg) from WHERE command."""
    resp = send_cmd(s, "WHERE")
    if not resp.startswith("OK WHERE"):
        raise RuntimeError(f"Unexpected WHERE response: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])


def robot_moveto(s: socket.socket, rx: float, ry: float) -> tuple[float, float]:
    """Send MOVETO, return actual (x, y) the robot reached."""
    resp = send_cmd(s, f"MOVETO {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK MOVETO"):
        raise RuntimeError(f"Unexpected MOVETO response: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


def robot_ring_on(s: socket.socket) -> None:
    resp = send_cmd(s, "RING_ON")
    if "OK" not in resp:
        print(f"  [WARN] RING_ON: {resp}")


def robot_ring_off(s: socket.socket) -> None:
    try:
        send_cmd(s, "RING_OFF")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# EoA camera helpers
# ══════════════════════════════════════════════════════════════════════════════

def eoa_connect():
    """
    Connect to EoA camera via arena_api.
    Returns (arena_system, device).
    """
    try:
        from arena_api.system import system as _sys
    except ImportError:
        print("  [ERROR] arena_api not found. Use cib_env python.")
        sys.exit(1)

    print(f"  Searching for EoA camera (serial {EOA_SERIAL}) ...")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if any(str(i.get("serial", "")) == EOA_SERIAL
               for i in list(_sys.device_infos)):
            break
        time.sleep(0.5)

    eoa_info = next(
        (i for i in list(_sys.device_infos)
         if str(i.get("serial", "")) == EOA_SERIAL), None
    )
    if eoa_info is None:
        print(f"  [ERROR] EoA camera serial {EOA_SERIAL} not found.")
        print("  Run force_camera_ip.py first.")
        sys.exit(1)

    devices = _sys.create_device(device_infos=eoa_info)
    device  = devices[0]
    nm = device.nodemap
    try:
        nm["PixelFormat"].value  = "BGR8"
    except Exception:
        pass
    try:
        nm["ExposureAuto"].value = "Off"
        nm["ExposureTime"].value = float(EOA_EXPOSURE)
    except Exception:
        pass
    try:
        nm["GainAuto"].value = "Continuous"
    except Exception:
        pass

    device.start_stream(4)
    print(f"  EoA camera connected. ip={eoa_info.get('ip')}")
    return _sys, device


def eoa_disconnect(arena_system, device) -> None:
    try:
        device.stop_stream()
    except Exception:
        pass
    try:
        arena_system.destroy_device()
    except Exception:
        pass


def grab_frame(device) -> np.ndarray:
    """Grab one frame from the EoA camera. Returns BGR uint8 array."""
    buf  = device.get_buffer(timeout=3000)
    bpp  = buf.bits_per_pixel // 8
    h, w = buf.height, buf.width
    raw  = (ctypes.c_uint8 * (h * w * bpp)).from_address(
                ctypes.addressof(buf.pbytes))
    flat = np.ctypeslib.as_array(raw).copy()
    device.requeue_buffer(buf)
    if bpp == 1:
        return cv2.cvtColor(flat.reshape(h, w), cv2.COLOR_GRAY2BGR)
    return flat.reshape(h, w, bpp)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Overhead homography
# ══════════════════════════════════════════════════════════════════════════════

def phase1_overhead(
    s: socket.socket,
    corners_px: list[tuple[float, float]],
) -> tuple[np.ndarray, float, float, float]:
    """
    Visit 5 checkerboard corners, compute overhead homography H.
    Returns (H, c2_x, c2_y, c2_u) where c2_* is the robot position at C2.
    """
    print()
    print("╔" + "═" * 58 + "╗")
    print("║  PHASE 1 — Overhead Homography Calibration" + " " * 15 + "║")
    print("╚" + "═" * 58 + "╝")
    print("""
CHECKLIST:
  ✓ Checkerboard is flat in the dish (same as Step 1).
  ✓ Robot Z is at safe working height — script never changes Z.
  ✓ Open  output/corners_numbered.png  to see which corner is C2.
  ✓ Run   eoa_live_feed.py  in a SEPARATE terminal for the crosshair.
""")

    print("  Key corners (pixel positions in overhead image):")
    for name, idx in IDX.items():
        px, py = corners_px[idx]
        tag = "  ← JOG HERE FIRST" if name == "C2" else ""
        print(f"    {name:3s}: pixel ({px:.0f}, {py:.0f}){tag}")

    print()
    print("─" * 60)
    print("JOG the robot so the EoA crosshair is exactly on C2.")
    print("Use the TEACH PENDANT.  Do NOT change Z or U.")
    input("Press ENTER when crosshair is centred on C2 ...")

    c2_x, c2_y, c2_z, c2_u = robot_where(s)
    print(f"\n  C2 recorded:  X={c2_x:.3f}  Y={c2_y:.3f}  Z={c2_z:.3f}  U={c2_u:.3f}°")

    # Compute target positions for all 5 corners from C2 offset
    targets = {"C2": (c2_x, c2_y)}
    for name, (dx, dy) in OFFSETS_FROM_C2.items():
        targets[name] = (c2_x + dx, c2_y + dy)

    print("\n  Planned corner positions (robot mm  ↔  pixel):")
    for name, (tx, ty) in targets.items():
        px, py = corners_px[IDX[name]]
        print(f"    {name:3s}: robot ({tx:+.1f}, {ty:+.1f})  ↔  pixel ({px:.0f}, {py:.0f})")

    input("\nPress ENTER to start AUTO-SCAN (robot will move) ...")
    print()

    pixel_pts: list[tuple[float, float]] = []
    robot_pts: list[tuple[float, float]] = []

    # C2 is already visited — record it first
    px2, py2 = corners_px[IDX["C2"]]
    pixel_pts.append((px2, py2))
    robot_pts.append((c2_x, c2_y))
    print(f"  C2  (start)  robot ({c2_x:.2f}, {c2_y:.2f})  pixel ({px2:.0f}, {py2:.0f})")

    for name in VISIT_ORDER:
        tx, ty = targets[name]
        px, py = corners_px[IDX[name]]
        print(f"  {name:3s}  moving  → ({tx:.2f}, {ty:.2f}) mm ...", end="  ", flush=True)
        try:
            actual_x, actual_y = robot_moveto(s, tx, ty)
        except Exception as exc:
            print(f"\n  [ERROR] MOVETO failed: {exc}")
            print("  Stopping. Re-run from the beginning.")
            sys.exit(1)
        delta = math.sqrt((actual_x - tx) ** 2 + (actual_y - ty) ** 2)
        pixel_pts.append((px, py))
        robot_pts.append((actual_x, actual_y))
        print(f"reached ({actual_x:.2f}, {actual_y:.2f})  Δ={delta:.2f}mm  ✓")

    # Return to C2 for Phase 2
    print(f"\n  Returning to C2 ({c2_x:.2f}, {c2_y:.2f}) ...")
    robot_moveto(s, c2_x, c2_y)

    # ── Compute H ─────────────────────────────────────────────────────────────
    print(f"\n  Computing homography from {len(pixel_pts)} pairs ...")
    px_arr = np.array(pixel_pts, dtype=np.float32)
    rb_arr = np.array(robot_pts,  dtype=np.float32)
    H, mask = cv2.findHomography(px_arr, rb_arr, cv2.RANSAC, 5.0)
    if H is None:
        print("  [ERROR] findHomography failed — points may be collinear.")
        sys.exit(1)

    inliers = int(mask.sum()) if mask is not None else len(pixel_pts)
    print(f"  Inliers: {inliers}/{len(pixel_pts)}")

    print("\n  Reprojection check  (target RMSE < 0.5mm):")
    errors = []
    for (px, py), (rx, ry) in zip(pixel_pts, robot_pts):
        pt   = np.array([[[px, py]]], dtype=np.float32)
        pred = cv2.perspectiveTransform(pt, H)[0][0]
        err  = math.sqrt((pred[0] - rx) ** 2 + (pred[1] - ry) ** 2)
        errors.append(err)
        print(f"    pixel ({px:.0f},{py:.0f})  pred ({pred[0]:.2f},{pred[1]:.2f})"
              f"  actual ({rx:.2f},{ry:.2f})  err={err:.3f}mm")

    rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
    grade = "EXCELLENT" if rmse < 0.5 else ("ACCEPTABLE" if rmse < 1.0 else
            "HIGH — re-run, position C2 more carefully")
    print(f"\n  RMSE: {rmse:.3f}mm  [{grade}]")

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(OUT_DIR, exist_ok=True)
    np.save(H_LOCAL, H)
    print(f"\n  Saved: {H_LOCAL}")
    try:
        os.makedirs(os.path.dirname(H_RUNTIME), exist_ok=True)
        shutil.copy(H_LOCAL, H_RUNTIME)
        print(f"  Copied to runtime: {H_RUNTIME}")
    except Exception as exc:
        print(f"  [WARN] Could not copy to runtime path: {exc}")

    pairs_path = os.path.join(OUT_DIR, "calibration_pairs.json")
    with open(pairs_path, "w") as f:
        json.dump({
            "rmse_mm": round(rmse, 4),
            "pairs": [
                {"pixel_x": float(px), "pixel_y": float(py),
                 "robot_x": float(rx), "robot_y": float(ry)}
                for (px, py), (rx, ry) in zip(pixel_pts, robot_pts)
            ],
        }, f, indent=2)
    print(f"  Pairs saved: {pairs_path}")

    return H, c2_x, c2_y, c2_u


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — EoA camera tool offset
# ══════════════════════════════════════════════════════════════════════════════

def _draw_overlay(
    display: np.ndarray,
    n_recorded: int,
    status_line: str,
    flash_text: str | None,
) -> None:
    """Draw crosshair + banner + optional flash text on the display frame."""
    dh, dw = display.shape[:2]
    cx, cy  = dw // 2, dh // 2

    # Crosshair
    arm = 45
    cv2.line(display, (cx - arm, cy), (cx + arm, cy), (0, 255, 0), 2)
    cv2.line(display, (cx, cy - arm), (cx, cy + arm), (0, 255, 0), 2)
    cv2.circle(display, (cx, cy), 10, (0, 255, 0), -1)
    cv2.circle(display, (cx, cy), arm // 2, (0, 255, 0), 1)

    # Top banner background
    cv2.rectangle(display, (0, 0), (dw, 70), (20, 20, 20), -1)
    cv2.putText(display, status_line,
                (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 210, 255), 2)
    cv2.putText(display, "SPACE = record this position  |  D = done  |  Q = abort",
                (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (140, 140, 140), 1)

    # Recorded count (top-right)
    label = f"Recorded: {n_recorded}"
    (lw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    cv2.putText(display, label, (dw - lw - 12, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 120), 2)

    # Flash overlay (shown briefly after each SPACE press)
    if flash_text:
        cv2.rectangle(display, (0, dh // 2 - 55), (dw, dh // 2 + 55),
                      (0, 60, 0), -1)
        (fw, _), _ = cv2.getTextSize(flash_text, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 3)
        cv2.putText(display, flash_text,
                    ((dw - fw) // 2, dh // 2 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 80), 3)


def phase2_tool_offset(
    c2_x: float,
    c2_y: float,
    c2_u: float,
) -> dict | None:
    """
    Manual-entry EoA tool offset calibration.

    You jog the robot to different U angles while watching eoa_live_feed.py
    in a separate terminal, then type the X, Y, U values from the teach pendant.

    Solves for (cx, cy) = camera offset from U rotation axis in end-effector frame.
    Returns the result dict, or None if aborted / not enough measurements.
    """
    print()
    print("╔" + "═" * 58 + "╗")
    print("║  PHASE 2 — EoA Camera Tool Offset Calibration" + " " * 11 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"""
HOW IT WORKS
  Measurement #1 is already recorded from Phase 1:
    X={c2_x:.3f}  Y={c2_y:.3f}  U={c2_u:.3f}°

  For each additional measurement:
    1. Open eoa_live_feed.py in a SEPARATE terminal  (for live crosshair view).
    2. Pause the SPEL+ program in RC+ (|| button), switch to TEACH mode.
    3. Jog to a DIFFERENT U angle.  Jog XY until EoA crosshair is on C2.
    4. Read X, Y, U off the teach pendant display.
    5. Switch back to AUTO, resume the program, then type the values here.

  You need at least 1 additional measurement (2 total).
  More measurements (3-4) at widely different U angles = better accuracy.

  Leave X blank and press ENTER to finish entering measurements.
""")

    # Pre-populate first measurement from Phase 1
    measurements: list[tuple[float, float, float]] = [(c2_x, c2_y, c2_u)]
    print(f"  Measurement 1 (from Phase 1): X={c2_x:.3f}  Y={c2_y:.3f}  U={c2_u:.3f}°")
    print()

    while True:
        n = len(measurements)
        print(f"─── Measurement {n + 1} (leave X blank to finish) ───")
        x_str = input("  X (mm): ").strip()
        if x_str == "":
            if n < 2:
                print(f"  Need at least 2 measurements total (have {n}). Enter one more.")
                continue
            break
        try:
            wx  = float(x_str)
            wy  = float(input("  Y (mm): ").strip())
            u   = float(input("  U (deg): ").strip())
        except ValueError:
            print("  Invalid number — try again.")
            continue

        measurements.append((wx, wy, u))
        print(f"  Recorded: X={wx:.3f}  Y={wy:.3f}  U={u:.3f}°  ({len(measurements)} total)")
        print()

    if len(measurements) < 2:
        print("  Not enough measurements — Phase 2 skipped.")
        return None

    # ── Check U angles are sufficiently spread ────────────────────────────────
    u_angles = [m[2] for m in measurements]
    u_spread = max(u_angles) - min(u_angles)
    if u_spread < 5.0:
        print(f"\n  [WARN] U angle spread is only {u_spread:.1f}°  (need > 5°).")
        print("  The solve will be poorly conditioned.")
        print("  Re-run Phase 2 with more distinct U angles for a reliable result.")

    # ── Solve for camera offset (cx, cy) via least squares ───────────────────
    #
    # Why 4 unknowns, not 2?
    # ----------------------
    # H was calibrated by mapping overhead pixels to WRIST positions (WHERE
    # values), not true world positions.  So we cannot derive C2's true world
    # position from H — it is unknown and must be solved for simultaneously.
    #
    # 4 unknowns: [cx, cy, P, Q]
    #   cx, cy = camera offset from U axis in end-effector frame (what we want)
    #   P, Q   = true world XY of C2 (solved as a side effect, discarded after)
    #
    # For each measurement i, when the camera crosshair is on C2:
    #   cam_world_x = wx_i + cx*cos(u_i) - cy*sin(u_i) = P   →   cos(u_i)*cx - sin(u_i)*cy - P = -wx_i
    #   cam_world_y = wy_i + cx*sin(u_i) + cy*cos(u_i) = Q   →   sin(u_i)*cx + cos(u_i)*cy - Q = -wy_i
    #
    # With N≥2 measurements: 2N equations, 4 unknowns → solvable (lstsq for N>2).
    print(f"\n  Solving camera offset from {len(measurements)} measurements ...")
    print(f"  U angles used: {[round(m[2], 1) for m in measurements]}")
    print()

    A_rows: list[list[float]] = []
    b_vals: list[float]       = []
    for wx, wy, u_deg in measurements:
        u = math.radians(u_deg)
        # [cx,          cy,           P,    Q  ]
        A_rows.append([ math.cos(u), -math.sin(u), -1.0,  0.0])
        b_vals.append(-wx)
        A_rows.append([ math.sin(u),  math.cos(u),  0.0, -1.0])
        b_vals.append(-wy)

    A = np.array(A_rows, dtype=float)
    b = np.array(b_vals, dtype=float)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    cx, cy, P_solved, Q_solved = sol

    # ── Verification: each measurement should predict the same world point ────
    print(f"  C2 true world position (solved): ({P_solved:.3f}, {Q_solved:.3f}) mm")
    print()
    print("  Verification (each wrist + offset should land on the same world point):")
    errors = []
    for wx, wy, u_deg in measurements:
        u     = math.radians(u_deg)
        cam_x = wx + cx * math.cos(u) - cy * math.sin(u)
        cam_y = wy + cx * math.sin(u) + cy * math.cos(u)
        err   = math.sqrt((cam_x - P_solved) ** 2 + (cam_y - Q_solved) ** 2)
        errors.append(err)
        print(f"    U={u_deg:7.2f}°  wrist ({wx:.2f},{wy:.2f})"
              f"  cam ({cam_x:.2f},{cam_y:.2f})  err={err:.3f}mm")

    rmse  = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
    grade = "EXCELLENT" if rmse < 0.5 else ("GOOD" if rmse < 1.5 else
            "HIGH — jog more carefully and re-run")
    print(f"\n  Camera offset from U axis:  cx={cx:.4f}mm  cy={cy:.4f}mm")
    print(f"  RMSE: {rmse:.3f}mm  [{grade}]")

    spel_cmd = f"Tool 1, {cx:.4f}, {cy:.4f}, 0, 0, 0, 0"

    # ── Save ──────────────────────────────────────────────────────────────────
    result = {
        "cx_mm":              round(cx, 4),
        "cy_mm":              round(cy, 4),
        "rmse_mm":            round(rmse, 4),
        "C2_world_mm":        [round(P_solved, 4), round(Q_solved, 4)],
        "u_spread_deg":       round(u_spread, 2),
        "n_measurements":     len(measurements),
        "measurements":       [
            {"wx_mm": round(wx, 4), "wy_mm": round(wy, 4), "u_deg": round(u, 4)}
            for wx, wy, u in measurements
        ],
        "spel_tool_command":  spel_cmd,
        "note": (
            "Give spel_tool_command to David — enter it via RC+ Setup → Robots → "
            "Robot 1 → Configuration → Tool, or run the command in a SPEL+ program."
        ),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(TOL_LOCAL, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {TOL_LOCAL}")
    try:
        os.makedirs(os.path.dirname(TOL_RUNTIME), exist_ok=True)
        shutil.copy(TOL_LOCAL, TOL_RUNTIME)
        print(f"  Copied to runtime: {TOL_RUNTIME}")
    except Exception as exc:
        print(f"  [WARN] Could not copy to runtime path: {exc}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔" + "═" * 58 + "╗")
    print("║  CIB Full Calibration  (Overhead H + EoA Tool Offset)" + "  ║")
    print("╚" + "═" * 58 + "╝")
    print("""
BEFORE STARTING:
  1. Load  calibration_v2_server.prg  in RC+ and press F5 to run.
     (NOT calibration_server.prg — use the _v2 version)
  2. Run   step1_detect_corners.py  first if not already done.
  3. Place checkerboard flat in the dish.
  4. Open  output/corners_numbered.png  to identify C2.
  5. Have  eoa_live_feed.py  ready to run in a second terminal.
""")
    input("Press ENTER when ready ...")

    # ── Load corners ──────────────────────────────────────────────────────────
    if not os.path.exists(CORNERS_JSON):
        print(f"\nERROR: {CORNERS_JSON} not found.")
        print("Run step1_detect_corners.py first.")
        sys.exit(1)

    with open(CORNERS_JSON) as f:
        data = json.load(f)
    corners_px = [(c["x_px"], c["y_px"]) for c in data["corners_px"]]
    print(f"\nLoaded {len(corners_px)} pixel corners from Step 1.")

    # ── Connect to robot ──────────────────────────────────────────────────────
    s = robot_connect()
    resp = send_cmd(s, "STATUS")
    if "READY" not in resp:
        print(f"Unexpected STATUS response: {resp!r}")
        print("Is calibration_v2_server.prg loaded and running?")
        s.close()
        sys.exit(1)
    print(f"  Robot: {resp}")

    tool_result = None

    try:
        # ── Phase 1 ───────────────────────────────────────────────────────────
        H, c2_x, c2_y, c2_u = phase1_overhead(s, corners_px)

        print()
        print(f"  Phase 1 complete.  H saved.  Robot at C2 ({c2_x:.2f}, {c2_y:.2f})  U={c2_u:.1f}°")

        # ── Phase 2 ───────────────────────────────────────────────────────────
        ans = input("\nProceed to Phase 2 (EoA tool offset)? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            tool_result = phase2_tool_offset(c2_x, c2_y, c2_u)
        else:
            print("  Phase 2 skipped — overhead H is still saved.")

    finally:
        try:
            send_cmd(s, "BYE")
        except Exception:
            pass
        s.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("╔" + "═" * 58 + "╗")
    print("║  CALIBRATION SUMMARY" + " " * 37 + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"  Overhead H    : {H_LOCAL}")
    print(f"  Runtime H     : {H_RUNTIME}")

    if tool_result:
        print()
        print(f"  Camera offset : cx={tool_result['cx_mm']:.4f}mm  "
              f"cy={tool_result['cy_mm']:.4f}mm")
        print(f"  RMSE          : {tool_result['rmse_mm']:.3f}mm")
        print(f"  Tool JSON     : {TOL_LOCAL}")
        print()
        print("  ┌─ GIVE THIS TO DAVID ──────────────────────────────────┐")
        print(f"  │  {tool_result['spel_tool_command']:<55}│")
        print("  │  Enter in RC+:  Setup → Robots → Robot 1 → Config    │")
        print("  └───────────────────────────────────────────────────────┘")
        if tool_result['rmse_mm'] > 1.5:
            print()
            print("  [WARN] RMSE is high. Re-run Phase 2, jog more carefully,")
            print("         and use more widely-spaced U angles.")
    else:
        print()
        print("  Tool offset: not computed (Phase 2 skipped or aborted).")
        print("  Run this script again and complete Phase 2 when ready.")

    print()


if __name__ == "__main__":
    main()
