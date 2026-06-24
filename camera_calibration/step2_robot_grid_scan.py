"""
STEP 2 — Robot visits key corners to compute the homography.

HOW IT WORKS
  1. You manually jog the EoA so the pipette tip is over C2 (centre inner corner).
  2. Press ENTER.  Script reads WHERE (robot XY at C2).
  3. Robot auto-moves to C1, C4, C0, C3 using fixed 20mm-grid offsets.
     Positions are recorded automatically — you do NOT need to write anything down.
  4. Homography H is computed from the 5 pixel<->robot pairs.
  5. H saved to output/ AND C:\\LabAutomation\\config\\

CORNER OFFSETS FROM C2 (centre, col=3 row=2 in 6x5 grid, 20mm squares)
  C1 top-right  :  x - 40 mm,  y + 40 mm
  C4 bot-right  :  x + 40 mm,  y + 40 mm
  C0 top-left   :  x - 40 mm,  y - 60 mm
  C3 bot-left   :  x + 40 mm,  y - 60 mm
  (robot: x- = toward top of dish, +y = toward right of dish)

BEFORE RUNNING
  - Load calibration_server.prg in RC+ 7.0 and press F5 to run it.
  - Make sure Z is at a safe working height.  Script never changes Z.
  - Open corners_numbered.png so you can see which corner is C2.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step2_robot_grid_scan.py
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
H_LOCAL      = os.path.join(OUT_DIR, "calibration_overhead.npy")
H_RUNTIME    = r"C:\LabAutomation\config\calibration_overhead.npy"

# ── Robot ─────────────────────────────────────────────────────────────────────
ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 15.0   # seconds — give robot time to finish each move

# ── Grid geometry (must match printed target + step1) ─────────────────────────
SQUARE_MM  = 20     # physical square size mm

# Corner indices in the 6-col x 5-row inner-corner array (from step1)
# C2 is at col=3, row=2  (index 15)
# We use col=1 for the left corners (40mm left) instead of col=0 (60mm left)
# to stay within the robot's joint limits.
# All 4 surrounding corners are exactly +-40mm from C2 in both axes.
#
#   col=1,row=0 = index  1   (top-left,  40mm left + 40mm top)
#   col=5,row=0 = index  5   (top-right, 40mm right + 40mm top)   <- C1
#   col=3,row=2 = index 15   (centre)                              <- C2
#   col=1,row=4 = index 25   (bot-left,  40mm left + 40mm bottom)
#   col=5,row=4 = index 29   (bot-right, 40mm right + 40mm bottom) <- C4
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
    "TR": (-40, +40),   # top-right
    "BR": (+40, +40),   # bot-right
    "TL": (-40, -40),   # top-left  (was -60, now -40 — avoids joint limit)
    "BL": (+40, -40),   # bot-left
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
    """Send MOVETO, return actual (x, y) reached."""
    resp = send_cmd(s, f"MOVETO {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK MOVETO"):
        raise RuntimeError(f"Unexpected MOVETO response: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("CIB Camera Calibration — Step 2: Robot Corner Scan")
    print("=" * 60)

    print("""
BEFORE CONTINUING:
  1. calibration_server.prg is loaded in RC+ and RUNNING (F5).
  2. Checkerboard is still in the dish (same as Step 1).
  3. Robot Z is at a safe working height — script never changes Z.
  4. Open corners_numbered.png to find C2 (labelled CENTRE).
""")

    # ── Load pixel corners ────────────────────────────────────────────────────
    if not os.path.exists(CORNERS_JSON):
        print(f"ERROR: {CORNERS_JSON} not found — run step1_detect_corners.py first.")
        sys.exit(1)

    with open(CORNERS_JSON) as f:
        data = json.load(f)
    corners_px = [(c["x_px"], c["y_px"]) for c in data["corners_px"]]
    print(f"Loaded {len(corners_px)} pixel corners from Step 1.")

    # Print where each key corner is so user can find them
    print("\n  Key corners (pixel positions in overhead image):")
    for name, idx in IDX.items():
        px, py = corners_px[idx]
        print(f"    {name}: pixel ({px:.1f}, {py:.1f})")

    # ── Connect to robot ──────────────────────────────────────────────────────
    try:
        s = robot_connect()
    except Exception as e:
        print(f"\nERROR: Cannot connect to robot — {e}")
        print(f"Check: robot on, IP {ROBOT_IP} reachable, calibration_server.prg running.")
        sys.exit(1)

    with s:
        resp = send_cmd(s, "STATUS")
        if "READY" not in resp:
            print(f"Unexpected STATUS: {resp!r}")
            sys.exit(1)
        print(f"  Robot: {resp}\n")

        # ── User jogs to C2 ───────────────────────────────────────────────────
        print("-" * 60)
        print("JOG robot so the EoA CAMERA CENTRE (crosshair in live feed)")
        print("is aligned over C2 (CENTRE inner corner of checkerboard).")
        print("Use the TEACH PENDANT.  Do NOT change Z.")
        print("Run eoa_live_feed.py in a separate window to see the crosshair.")
        input("Press ENTER when camera centre is aligned over C2 ...")

        c2_x, c2_y = robot_where(s)
        print(f"  C2 recorded:  X={c2_x:.3f} mm,  Y={c2_y:.3f} mm")

        # ── Build target positions for all 5 corners ───────────────────────────
        targets = {"C2": (c2_x, c2_y)}
        for name, (dx, dy) in OFFSETS_FROM_C2.items():
            targets[name] = (c2_x + dx, c2_y + dy)

        print("\n  Computed target positions:")
        for name, (tx, ty) in targets.items():
            px, py = corners_px[IDX[name]]
            print(f"    {name}: robot ({tx:.2f}, {ty:.2f}) mm  "
                  f"<->  pixel ({px:.1f}, {py:.1f})")

        input("\nPress ENTER to start auto-scan (robot will move) ...")

        # ── Visit each corner ─────────────────────────────────────────────────
        print("\n" + "-" * 60)
        print("AUTO-SCAN ...")
        print("-" * 60)

        pixel_pts = []
        robot_pts = []

        # Record C2 first (already there, no move needed)
        px2, py2 = corners_px[IDX["C2"]]
        pixel_pts.append((px2, py2))
        robot_pts.append((c2_x, c2_y))
        print(f"  C2 (start)  robot ({c2_x:.2f},{c2_y:.2f})  pixel ({px2:.1f},{py2:.1f})")

        # Visit the other 4 corners
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

        # Return to C2
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

    # ── Save ──────────────────────────────────────────────────────────────────
    np.save(H_LOCAL, H)
    print(f"\nSaved: {H_LOCAL}")

    os.makedirs(os.path.dirname(H_RUNTIME), exist_ok=True)
    shutil.copy(H_LOCAL, H_RUNTIME)
    print(f"Copied to runtime: {H_RUNTIME}")

    # Save pairs for reference
    pairs_path = os.path.join(OUT_DIR, "calibration_pairs.json")
    with open(pairs_path, "w") as f:
        json.dump({
            "rmse_mm": rmse,
            "pairs": [
                {"pixel_x": float(px), "pixel_y": float(py),
                 "robot_x": float(rx), "robot_y": float(ry)}
                for (px, py), (rx, ry) in zip(pixel_pts, robot_pts)
            ]
        }, f, indent=2)
    print(f"Saved pairs: {pairs_path}")

    print("\n" + "=" * 60)
    if rmse < 1.0:
        print(f"CALIBRATION COMPLETE  (RMSE={rmse:.3f} mm)")
        print("Next: run step3_verify.py to spot-check accuracy.")
    else:
        print(f"RMSE={rmse:.3f} mm — position C2 more carefully and re-run.")
    print("=" * 60)


if __name__ == "__main__":
    main()
