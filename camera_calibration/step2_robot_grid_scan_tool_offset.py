"""
STEP 2 (tool-offset version) — Robot visits key corners to compute the
homography, with Tool 1 active — so H maps pixel -> CAMERA position,
not wrist position.

WHY A SEPARATE SCRIPT
  step2_robot_grid_scan.py (original) drives calibration_server.prg /
  calibration_v2_server.prg, neither of which activates Tool 1 — so it
  records raw WRIST positions. That was fine until sort_server.prg started
  activating Tool 1 for every MOVE_IMAGE, which now interprets the same H
  output as a CAMERA target instead. Result: a constant ~79mm position
  error (the size of the tool offset itself), independent of which U you
  test at.

  This script fixes that by driving calibration_v3_tooloffset_server.prg,
  which sets Tool 1 (cx=17.1876, cy=-77.0233 — same as sort_server.prg) at
  startup. WHERE / MOVETO then report/target the CAMERA position, so the
  resulting H maps pixel -> camera position directly. Because Tool 1
  handles U-compensation internally, this ONE calibration (done at any
  single fixed U) is valid at every U/quadrant afterward.

DOES U MATTER?
  No particular U or quadrant is required — pick any U where the robot can
  comfortably reach all 5 grid points (C2 +-40mm in X/Y), and hold it fixed
  for the whole scan (don't rotate U mid-scan). Tool 1 compensation is what
  makes the result valid everywhere afterward, not the specific U you
  calibrate at.

SAFETY — DOES NOT OVERWRITE THE OLD CALIBRATION
  This saves to a NEW file (calibration_overhead_tooloffset.npy) so the
  existing calibration_overhead.npy is untouched until you've checked the
  new RMSE and explicitly promoted it (see PROMOTE step printed at the end).

HOW IT WORKS
  1. You manually jog the EoA so the camera crosshair is over C2 (centre
     inner corner). Use eoa_live_feed.py to see the crosshair.
  2. Press ENTER. Script reads WHERE (camera XY at C2, Tool 1 active).
  3. Robot auto-moves to C1, C4, C0, C3 using fixed 40mm-grid offsets.
  4. Homography H is computed from the 5 pixel<->camera pairs.
  5. H saved to output/calibration_overhead_tooloffset.npy (NOT the
     production path — promote manually once verified).

BEFORE RUNNING
  - Load calibration_v3_tooloffset_server.prg in RC+ 7.0 and press F5.
  - Do NOT run sort_server.prg / calibration_server.prg / calibration_v2_server.prg
    at the same time.
  - Make sure Z is at a safe working height. Script never changes Z.
  - Open corners_numbered.png so you can see which corner is C2.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step2_robot_grid_scan_tool_offset.py
"""

import json
import os
import shutil
import socket
import sys
import numpy as np
import cv2

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_DIR      = os.path.join(os.path.dirname(__file__), "output")
CORNERS_JSON = os.path.join(OUT_DIR, "corners.json")

# NEW output — does NOT touch the existing calibration_overhead.npy
H_NEW        = os.path.join(OUT_DIR, "calibration_overhead_tooloffset.npy")
PAIRS_NEW    = os.path.join(OUT_DIR, "calibration_pairs_tooloffset.json")

# Existing production paths — only touched by the PROMOTE step, manually
H_PROD_LOCAL   = os.path.join(OUT_DIR, "calibration_overhead.npy")
H_PROD_RUNTIME = r"C:\LabAutomation\config\calibration_overhead.npy"

# ── Robot ─────────────────────────────────────────────────────────────────────
ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 15.0   # seconds — give robot time to finish each move

# ── Grid geometry (must match printed target + step1) ─────────────────────────
SQUARE_MM  = 20     # physical square size mm

IDX = {
    "TL": 1,    # top-left  (col=1, row=0)
    "TR": 5,    # top-right (col=5, row=0)  = C1
    "C2": 15,   # centre    (col=3, row=2)
    "BL": 25,   # bot-left  (col=1, row=4)
    "BR": 29,   # bot-right (col=5, row=4)  = C4
}

# Offsets FROM C2 in robot XY (mm) — all +-40mm, symmetric.
# Robot: x- = toward top,  +y = toward right
OFFSETS_FROM_C2 = {
    "TR": (-40, +40),
    "BR": (+40, +40),
    "TL": (-40, -40),
    "BL": (+40, -40),
}

VISIT_ORDER = ["TR", "BR", "TL", "BL"]


# ═══════════════════════════════════════════════════════════════════════════════
# Robot TCP helpers
# ═══════════════════════════════════════════════════════════════════════════════

def robot_connect() -> socket.socket:
    print(f"Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((ROBOT_IP, ROBOT_PORT))
    print("  Connected.")
    return s


def recv_line(s: socket.socket) -> str:
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
    return recv_line(s)


def robot_where(s: socket.socket) -> tuple:
    resp = send_cmd(s, "WHERE")
    if not resp.startswith("OK WHERE"):
        raise RuntimeError(f"Unexpected WHERE response: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


def robot_moveto(s: socket.socket, rx: float, ry: float) -> tuple:
    """Send MOVETO, return actual (x, y) reached (camera position, Tool 1 active)."""
    resp = send_cmd(s, f"MOVETO {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK MOVETO"):
        raise RuntimeError(f"Unexpected MOVETO response: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("CIB Camera Calibration — Step 2 (TOOL OFFSET version)")
    print("=" * 70)

    print("""
BEFORE CONTINUING:
  1. calibration_v3_tooloffset_server.prg is loaded in RC+ and RUNNING (F5).
     (NOT calibration_server.prg / calibration_v2_server.prg / sort_server.prg)
  2. Checkerboard is still in the dish (same as Step 1).
  3. Robot Z is at a safe working height — script never changes Z.
  4. Open corners_numbered.png to find C2 (labelled CENTRE).
  5. Run eoa_live_feed.py in a separate window to see the crosshair.

NOTE ON U: any single U angle works, as long as the robot can reach all
5 grid points from it and you don't rotate U mid-scan. Tool 1 handles
U-compensation internally — that's what makes this valid at every quadrant
afterward, not the specific U used here.

This will NOT overwrite the existing calibration_overhead.npy — it saves
to a new file so you can compare RMSE before promoting it.
""")

    # ── Load pixel corners ────────────────────────────────────────────────────
    if not os.path.exists(CORNERS_JSON):
        print(f"ERROR: {CORNERS_JSON} not found — run step1_detect_corners.py first.")
        sys.exit(1)

    with open(CORNERS_JSON) as f:
        data = json.load(f)
    corners_px = [(c["x_px"], c["y_px"]) for c in data["corners_px"]]
    print(f"Loaded {len(corners_px)} pixel corners from Step 1.")

    print("\n  Key corners (pixel positions in overhead image):")
    for name, idx in IDX.items():
        px, py = corners_px[idx]
        print(f"    {name}: pixel ({px:.1f}, {py:.1f})")

    # ── Connect to robot ──────────────────────────────────────────────────────
    try:
        s = robot_connect()
    except Exception as e:
        print(f"\nERROR: Cannot connect to robot — {e}")
        print(f"Check: robot on, IP {ROBOT_IP} reachable, "
              f"calibration_v3_tooloffset_server.prg running.")
        sys.exit(1)

    with s:
        resp = send_cmd(s, "STATUS")
        if "READY" not in resp:
            print(f"Unexpected STATUS: {resp!r}")
            sys.exit(1)
        print(f"  Robot: {resp}\n")

        # ── User jogs to C2 ───────────────────────────────────────────────────
        print("-" * 70)
        print("JOG robot so the EoA CAMERA CENTRE (crosshair in live feed)")
        print("is aligned over C2 (CENTRE inner corner of checkerboard).")
        print("Use the TEACH PENDANT. Do NOT change Z.")
        input("Press ENTER when camera centre is aligned over C2 ...")

        c2_x, c2_y = robot_where(s)
        print(f"  C2 recorded (camera position):  X={c2_x:.3f} mm,  Y={c2_y:.3f} mm")

        # ── Build target positions for all 5 corners ───────────────────────────
        targets = {"C2": (c2_x, c2_y)}
        for name, (dx, dy) in OFFSETS_FROM_C2.items():
            targets[name] = (c2_x + dx, c2_y + dy)

        print("\n  Computed target positions:")
        for name, (tx, ty) in targets.items():
            px, py = corners_px[IDX[name]]
            print(f"    {name}: camera ({tx:.2f}, {ty:.2f}) mm  "
                  f"<->  pixel ({px:.1f}, {py:.1f})")

        input("\nPress ENTER to start auto-scan (robot will move) ...")

        # ── Visit each corner ─────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("AUTO-SCAN ...")
        print("-" * 70)

        pixel_pts = []
        robot_pts = []

        px2, py2 = corners_px[IDX["C2"]]
        pixel_pts.append((px2, py2))
        robot_pts.append((c2_x, c2_y))
        print(f"  C2 (start)  camera ({c2_x:.2f},{c2_y:.2f})  pixel ({px2:.1f},{py2:.1f})")

        for name in VISIT_ORDER:
            tx, ty = targets[name]
            px, py = corners_px[IDX[name]]
            print(f"  Moving to {name} ({tx:.2f},{ty:.2f}) mm ...", end="  ", flush=True)

            try:
                actual_x, actual_y = robot_moveto(s, tx, ty)
            except Exception as e:
                print(f"FAILED: {e}")
                print("Stopping. Re-run from the beginning.")
                sys.exit(1)

            pixel_pts.append((px, py))
            robot_pts.append((actual_x, actual_y))

            delta = ((actual_x - tx)**2 + (actual_y - ty)**2) ** 0.5
            print(f"reached ({actual_x:.2f},{actual_y:.2f})  delta={delta:.2f}mm  OK")

        print(f"\n  Returning to C2 ({c2_x:.2f},{c2_y:.2f}) ...")
        robot_moveto(s, c2_x, c2_y)

        send_cmd(s, "BYE")
        print("  Robot disconnected.")

    # ── Compute homography ────────────────────────────────────────────────────
    print(f"\nComputing homography from {len(pixel_pts)} point pairs ...")
    px_arr = np.array(pixel_pts, dtype=np.float32)
    rb_arr = np.array(robot_pts,  dtype=np.float32)

    H, mask = cv2.findHomography(px_arr, rb_arr, cv2.RANSAC, 5.0)
    if H is None:
        print("ERROR: findHomography failed — check that the 5 points are not collinear.")
        sys.exit(1)

    inliers = int(mask.sum()) if mask is not None else len(pixel_pts)
    print(f"  Inliers: {inliers}/{len(pixel_pts)}")

    # ── Reprojection check ────────────────────────────────────────────────────
    print("\n  Reprojection check (target < 0.5 mm RMSE):")
    errors = []
    for (px, py), (rx, ry) in zip(pixel_pts, robot_pts):
        pt   = np.array([[[px, py]]], dtype=np.float32)
        pred = cv2.perspectiveTransform(pt, H)[0][0]
        err  = float(((pred[0]-rx)**2 + (pred[1]-ry)**2) ** 0.5)
        errors.append(err)
        print(f"    pixel ({px:.0f},{py:.0f}) -> predicted ({pred[0]:.2f},{pred[1]:.2f}) "
              f"actual ({rx:.2f},{ry:.2f})  err={err:.3f}mm")

    rmse = float(np.sqrt(np.mean(np.array(errors)**2)))
    print(f"\n  RMSE: {rmse:.3f} mm", end="  ")
    if rmse < 0.5:
        print("GOOD")
    elif rmse < 1.0:
        print("ACCEPTABLE")
    else:
        print("HIGH — re-run and position C2 more carefully")

    # ── Save to NEW file only — production path untouched ───────────────────
    np.save(H_NEW, H)
    print(f"\nSaved (NEW, not production): {H_NEW}")

    with open(PAIRS_NEW, "w") as f:
        json.dump({
            "rmse_mm": rmse,
            "pairs": [
                {"pixel_x": float(px), "pixel_y": float(py),
                 "camera_x": float(rx), "camera_y": float(ry)}
                for (px, py), (rx, ry) in zip(pixel_pts, robot_pts)
            ]
        }, f, indent=2)
    print(f"Saved pairs: {PAIRS_NEW}")

    print("\n" + "=" * 70)
    if rmse < 1.0:
        print(f"CALIBRATION COMPLETE  (RMSE={rmse:.3f} mm)")
        print("\nNext steps:")
        print("  1. Verify with test_quadrant_click.py — but first point it at the")
        print(f"     NEW file: change H_PATH to {H_NEW}")
        print("  2. Click points in all 4 quadrants, confirm the EoA crosshair lands")
        print("     correctly regardless of U — this is the real test.")
        print("  3. Once confirmed, PROMOTE by running:")
        print(f'       copy "{H_NEW}" "{H_PROD_LOCAL}"')
        print(f'       copy "{H_NEW}" "{H_PROD_RUNTIME}"')
    else:
        print(f"RMSE={rmse:.3f} mm — position C2 more carefully and re-run.")
    print("=" * 70)


if __name__ == "__main__":
    main()
