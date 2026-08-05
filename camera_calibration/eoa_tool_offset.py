"""
eoa_tool_offset.py — EoA Camera Tool Offset Calibration
========================================================

Standalone script — no robot program, no camera connection needed.
You jog the robot manually and type in the coordinates from the teach pendant.

WHAT THIS CALCULATES
  The EoA camera is mounted at a fixed offset (cx, cy) from the robot's
  U rotation axis (the wrist), in the end-effector frame.  When U changes
  to reach a different quadrant, the camera physically moves in world space
  by an amount that depends on (cx, cy) and the rotation angle.

  Once (cx, cy) is known and entered into RC+ as a Tool offset, the robot
  controller compensates automatically — MOVE_IMAGE goes to the right place
  in every quadrant without recalibrating.

HOW TO USE
  1. Run  eoa_live_feed.py  in a SEPARATE terminal so you can see the crosshair.
  2. Jog the robot so the EoA crosshair is on any fixed, sharp point
     (a checkerboard corner, a dot on paper, a pin — anything you can
     re-centre on precisely from multiple angles).
  3. Read X, Y, U from the teach pendant display. Type them in here.
  4. Change U to a different angle on the teach pendant.
     Jog XY until the crosshair is back on the SAME point.
     Read X, Y, U. Type them in.
  5. Repeat for at least one more U angle (3 total is ideal).
  6. Leave X blank and press ENTER to finish.

  Script prints the SPEL+ Tool command — give it to David.

OUTPUTS
  camera_calibration/output/tool_offset.json
  C:\\LabAutomation\\config\\tool_offset.json  (copied automatically if dir exists)

USAGE
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/eoa_tool_offset.py
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys

import numpy as np

# ── Output paths ───────────────────────────────────────────────────────────────
OUT_DIR     = os.path.join(os.path.dirname(__file__), "output")
TOL_LOCAL   = os.path.join(OUT_DIR, "tool_offset.json")
TOL_RUNTIME = r"C:\LabAutomation\config\tool_offset.json"


# ══════════════════════════════════════════════════════════════════════════════
# Solve
# ══════════════════════════════════════════════════════════════════════════════

def solve_tool_offset(
    measurements: list[tuple[float, float, float]],
) -> tuple[float, float, float, float, float]:
    """
    Solve for camera offset (cx, cy) from N wrist measurements.

    Each measurement is (wx, wy, u_deg): the wrist XY position and U angle
    when the EoA camera crosshair was centred on the same fixed world point.

    Returns (cx, cy, P, Q, rmse_mm) where:
      cx, cy  = camera offset from U axis in end-effector frame (mm)
      P, Q    = solved world XY of the reference point (mm)
      rmse_mm = consistency error across all measurements

    Formulation — 4 unknowns [cx, cy, P, Q]:
      For each measurement i, camera world pos = wrist + R(u_i) @ [cx, cy] = [P, Q]
        cos(u_i)*cx - sin(u_i)*cy - P = -wx_i
        sin(u_i)*cx + cos(u_i)*cy - Q = -wy_i
      With N≥2 measurements: 2N equations, 4 unknowns → lstsq.
    """
    A_rows: list[list[float]] = []
    b_vals: list[float]       = []

    for wx, wy, u_deg in measurements:
        u = math.radians(u_deg)
        A_rows.append([ math.cos(u), -math.sin(u), -1.0,  0.0])
        b_vals.append(-wx)
        A_rows.append([ math.sin(u),  math.cos(u),  0.0, -1.0])
        b_vals.append(-wy)

    A   = np.array(A_rows, dtype=float)
    b   = np.array(b_vals, dtype=float)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    cx, cy, P, Q = sol

    errors = []
    for wx, wy, u_deg in measurements:
        u     = math.radians(u_deg)
        cam_x = wx + cx * math.cos(u) - cy * math.sin(u)
        cam_y = wy + cx * math.sin(u) + cy * math.cos(u)
        errors.append(math.sqrt((cam_x - P) ** 2 + (cam_y - Q) ** 2))

    rmse = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
    return cx, cy, P, Q, rmse


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔" + "═" * 60 + "╗")
    print("║  CIB — EoA Camera Tool Offset Calibration" + " " * 18 + "║")
    print("╚" + "═" * 60 + "╝")
    print("""
BEFORE STARTING
  • Run  eoa_live_feed.py  in a separate terminal — it shows the EoA
    crosshair so you know when you are centred on the reference point.
  • Pick any sharp fixed point to use as your reference:
      - a checkerboard corner (easiest — already on the dish)
      - a dot on paper
      - a pin or needle tip
  • The robot program does NOT need to be running.
    Jog freely in TEACH mode.

TIP: Use U angles that are as far apart as possible.
     e.g.  170°, 260°, 350°  gives much better accuracy than  170°, 175°, 180°.
""")
    input("Press ENTER when ready ...")
    print()

    # ── Collect measurements ───────────────────────────────────────────────────
    measurements: list[tuple[float, float, float]] = []

    while True:
        n = len(measurements)

        if n == 0:
            print("─── Measurement 1 — first U angle ───")
            print("  Jog to any U angle. Centre the EoA crosshair on your reference point.")
        else:
            print(f"─── Measurement {n + 1} — different U angle ───")
            print("  Jog to a DIFFERENT U angle. Re-centre crosshair on the SAME point.")

        x_str = input("  X (mm)  [blank = done]: ").strip()

        if x_str == "":
            if n < 2:
                print(f"  Need at least 2 measurements (have {n}). Enter one more.\n")
                continue
            break

        try:
            wx = float(x_str)
            wy = float(input("  Y (mm): ").strip())
            u  = float(input("  U (deg): ").strip())
        except ValueError:
            print("  Invalid number — try again.\n")
            continue

        # Warn if this U angle is too close to an existing one
        if measurements:
            min_sep = min(abs(u - m[2]) for m in measurements)
            # Handle wrap-around (e.g. 350° vs 10° = 20° apart, not 340°)
            min_sep = min(min_sep, 360 - min_sep)
            if min_sep < 10.0:
                print(f"  [WARN] U={u:.1f}° is only {min_sep:.1f}° away from a previous angle.")
                print("  Wider U separation gives more accurate results.")

        measurements.append((wx, wy, u))
        print(f"  ✓ Recorded  ({len(measurements)} total)\n")

    # ── Solve ──────────────────────────────────────────────────────────────────
    print()
    print("─" * 62)
    u_angles = [m[2] for m in measurements]
    u_spread = max(u_angles) - min(u_angles)
    # Handle wrap-around spread
    u_spread = min(u_spread, 360 - u_spread)

    print(f"  Measurements : {len(measurements)}")
    print(f"  U angles     : {[round(u, 1) for u in u_angles]}")
    print(f"  U spread     : {u_spread:.1f}°")

    if u_spread < 10.0:
        print()
        print("  [WARN] U spread < 10° — result may be unreliable.")
        print("  Re-run with U angles that are further apart.")

    print()
    cx, cy, P, Q, rmse = solve_tool_offset(measurements)

    print(f"  Reference point (solved world XY): ({P:.3f}, {Q:.3f}) mm")
    print()
    print("  Verification — each wrist + offset should hit the same world point:")
    for wx, wy, u_deg in measurements:
        u     = math.radians(u_deg)
        cam_x = wx + cx * math.cos(u) - cy * math.sin(u)
        cam_y = wy + cx * math.sin(u) + cy * math.cos(u)
        err   = math.sqrt((cam_x - P) ** 2 + (cam_y - Q) ** 2)
        print(f"    U={u_deg:7.2f}°  wrist ({wx:.3f}, {wy:.3f})"
              f"  cam ({cam_x:.3f}, {cam_y:.3f})  err={err:.3f}mm")

    print()
    if rmse < 0.5:
        grade = "EXCELLENT"
    elif rmse < 1.5:
        grade = "GOOD"
    else:
        grade = "HIGH — re-jog more carefully, or use wider U angles"

    print(f"  RMSE: {rmse:.3f}mm  [{grade}]")
    print()
    print(f"  Camera offset from U axis:  cx = {cx:.4f} mm   cy = {cy:.4f} mm")

    spel_cmd = f"Tool 1, {cx:.4f}, {cy:.4f}, 0, 0, 0, 0"

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {
        "cx_mm":             round(cx, 4),
        "cy_mm":             round(cy, 4),
        "rmse_mm":           round(rmse, 4),
        "u_spread_deg":      round(u_spread, 2),
        "reference_world_mm": [round(P, 4), round(Q, 4)],
        "n_measurements":    len(measurements),
        "measurements": [
            {"wx_mm": round(wx, 4), "wy_mm": round(wy, 4), "u_deg": round(u, 4)}
            for wx, wy, u in measurements
        ],
        "spel_tool_command": spel_cmd,
    }
    with open(TOL_LOCAL, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Saved: {TOL_LOCAL}")

    runtime_dir = os.path.dirname(TOL_RUNTIME)
    if os.path.isdir(runtime_dir):
        shutil.copy(TOL_LOCAL, TOL_RUNTIME)
        print(f"  Copied: {TOL_RUNTIME}")

    print()
    print("╔" + "═" * 60 + "╗")
    print("║  RESULT — give this SPEL+ command to David" + " " * 17 + "║")
    print("╠" + "═" * 60 + "╣")
    print(f"║  {spel_cmd:<59}║")
    print("╠" + "═" * 60 + "╣")
    print("║  Enter in RC+:                                             ║")
    print("║  Setup → Robots → Robot 1 → Configuration → Tool          ║")
    print("╚" + "═" * 60 + "╝")
    print()


if __name__ == "__main__":
    main()
