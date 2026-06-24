"""
STEP 3 — Verify calibration by driving robot to all 30 inner corners
using ONLY the homography H (pixel -> robot XY deprojection).

The robot does NOT use fixed 20mm offsets.
Every position is computed from the pixel coordinate via H.
Watch the EoA live feed (eoa_live_feed.py) — the crosshair should
land on each inner corner if calibration is good.

BEFORE RUNNING:
  - calibration_server.prg is running in RC+ (F5).
  - Checkerboard is still in the dish.
  - Z is at safe working height.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step3_verify_robot.py
"""

import json
import os
import sys
import socket
import numpy as np
import cv2

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_DIR      = os.path.join(os.path.dirname(__file__), "output")
CORNERS_JSON = os.path.join(OUT_DIR, "corners.json")
H_PATH       = os.path.join(OUT_DIR, "calibration_overhead.npy")

# ── Robot ─────────────────────────────────────────────────────────────────────
ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 15.0

# Pause between moves (seconds) — increase if robot doesn't finish in time
MOVE_SETTLE_S = 0.5


# ── Robot helpers ─────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((ROBOT_IP, ROBOT_PORT))
    return s


def recv_line(s: socket.socket) -> str:
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
    return recv_line(s)


def robot_moveto(s: socket.socket, rx: float, ry: float) -> tuple:
    resp = send_cmd(s, f"MOVETO {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK MOVETO"):
        raise RuntimeError(f"MOVETO failed: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


def robot_where(s: socket.socket) -> tuple:
    resp = send_cmd(s, "WHERE")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


# ── Homography ────────────────────────────────────────────────────────────────

def pixel_to_robot(px: float, py: float, H: np.ndarray) -> tuple:
    pt     = np.array([[[px, py]]], dtype=np.float32)
    result = cv2.perspectiveTransform(pt, H)
    return float(result[0][0][0]), float(result[0][0][1])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("CIB Calibration — Step 3: Robot visits all 30 corners via H")
    print("=" * 60)

    # Load H
    if not os.path.exists(H_PATH):
        print(f"ERROR: No H matrix at {H_PATH}")
        print("Run step2_robot_grid_scan.py first.")
        sys.exit(1)
    H = np.load(H_PATH)
    print(f"Loaded H matrix from: {H_PATH}")

    # Load pixel corners
    if not os.path.exists(CORNERS_JSON):
        print(f"ERROR: No corners.json at {CORNERS_JSON}")
        print("Run step1_detect_corners.py first.")
        sys.exit(1)
    with open(CORNERS_JSON) as f:
        data = json.load(f)
    corners_px = [(c["x_px"], c["y_px"]) for c in data["corners_px"]]
    total = len(corners_px)
    print(f"Loaded {total} pixel corners.")

    inner_cols = data["inner_cols"]   # 6
    inner_rows = data["inner_rows"]   # 5

    # Only visit corners at col >= 3 (C2 column and everything to the right).
    # Col 0,1,2 are too far left and hit the joint limit.
    # Index formula: row * inner_cols + col
    reachable = [
        i for i in range(total)
        if (i % inner_cols) >= 3
    ]
    print(f"Visiting {len(reachable)}/{total} corners "
          f"(col >= 3, right half including centre column).")

    # Convert reachable pixel corners to robot XY via H
    robot_targets = [(i, corners_px[i], pixel_to_robot(*corners_px[i], H))
                     for i in reachable]

    print(f"\nTarget robot positions (from H):")
    for i, (px, py), (rx, ry) in robot_targets:
        col = i % inner_cols
        row = i // inner_cols
        print(f"  [{i:2d}] col={col} row={row}  "
              f"pixel ({px:7.1f},{py:7.1f})  ->  robot ({rx:7.3f},{ry:7.3f}) mm")

    # Connect to robot
    print(f"\nConnecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    try:
        s = robot_connect()
    except Exception as e:
        print(f"ERROR: {e}")
        print("Check robot is on and calibration_server.prg is running.")
        sys.exit(1)

    with s:
        resp = send_cmd(s, "STATUS")
        if "READY" not in resp:
            print(f"Unexpected STATUS: {resp}")
            sys.exit(1)
        print("Robot ready.\n")

        # Record start position to return to at end
        start_x, start_y = robot_where(s)
        print(f"Start position: X={start_x:.3f}, Y={start_y:.3f}")

        print("\nRun eoa_live_feed.py in another window to watch the crosshair.")
        input(f"Press ENTER to start visiting {len(robot_targets)} corners ...")

        print("\n" + "-" * 60)
        errors = []
        import time

        for n, (i, (px, py), (rx, ry)) in enumerate(robot_targets):
            col = i % inner_cols
            row = i // inner_cols
            print(f"  [{n+1:2d}/{len(robot_targets)}] idx={i} col={col} row={row}  "
                  f"pixel ({px:.0f},{py:.0f})  ->  ({rx:.3f},{ry:.3f})",
                  end="  ", flush=True)

            try:
                actual_x, actual_y = robot_moveto(s, rx, ry)
            except Exception as e:
                print(f"FAILED: {e}")
                print("Stopping.")
                break

            delta = ((actual_x - rx)**2 + (actual_y - ry)**2) ** 0.5
            errors.append(delta)
            print(f"reached ({actual_x:.3f},{actual_y:.3f})  delta={delta:.3f}mm")
            time.sleep(MOVE_SETTLE_S)

        # Return to start
        print(f"\nReturning to start position ({start_x:.3f},{start_y:.3f}) ...")
        robot_moveto(s, start_x, start_y)

        send_cmd(s, "BYE")
        print("Robot disconnected.")

    # Summary
    if errors:
        rmse = float(np.sqrt(np.mean(np.array(errors)**2)))
        print("\n" + "=" * 60)
        print(f"Visited {len(errors)}/{len(robot_targets)} corners.")
        print(f"Motor delta RMSE: {rmse:.3f} mm  (max: {max(errors):.3f} mm)")
        print("\nIf the EoA crosshair landed on each corner in the live feed,")
        print("calibration is GOOD.")
        print("=" * 60)


if __name__ == "__main__":
    main()
