"""
eoa_calibrate_click.py — Interactive EoA Camera Calibration
============================================================

Same goal as eoa_calibrate.py but YOU click the bug instead of the
script trying to detect it automatically. Reliable when there are many
bugs in the dish.

HOW IT WORKS
------------
  1. Robot moves to the overhead centroid you give it.
  2. EoA image pops up — click on the bug body centre → press ENTER or SPACE.
  3. Robot moves +DELTA_MM in X — click same bug → confirm.
  4. Robot moves +DELTA_MM in Y (back to centre first) — click same bug → confirm.
  5. Script computes the full 2×2 pixel-to-mm matrix + camera-to-tool offset.
  6. Saves calibration_eoa.npy (same format as eoa_calibrate.py).

3 clicks total. No blob detection. No guessing.

CONTROLS (in the image window)
-------------------------------
  Left-click          — place/move the crosshair to the bug body centre
  SPACE or ENTER      — confirm this click and move to next position
  R                   — redo this image (re-capture)
  Q / ESC             — abort

USAGE
-----
  1. sort_server.prg running in RC+ (F5).
  2. Get overhead centroid XY from click_to_move.py or main.py logs.
  3. Run:
       C:\\Users\\TejasKumarLeelavathi\\miniconda3\\envs\\cib_env\\python.exe camera_calibration\\eoa_calibrate_click.py
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

import cv2
import numpy as np

# ── Settings (same as eoa_calibrate.py) ──────────────────────────────────────

ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
ROBOT_TIMEOUT = 30.0

EOA_SERIAL    = "253102169"
EXPOSURE_US   = 60000   # brighter than pipeline default (24995) — easier to click on bugs
GAIN_DB       = 0.0

DELTA_MM      = 5.0          # robot moves ±DELTA_MM for scale measurement

OUT_DIR       = os.path.join(os.path.dirname(__file__), "output")
CAL_LOCAL     = os.path.join(OUT_DIR, "calibration_eoa.npy")
CAL_RUNTIME   = r"C:\LabAutomation\config\calibration_eoa.npy"

# Display: scale down the 5320×4600 image for the click window
# Show only the central 40% of the image at 35% scale.
# Full image 5320×4600 → crop to central 2128×1840 → display at ~745×644.
# This keeps the window small while showing the bug area sharply.
DISPLAY_CROP_FRAC = 0.40   # fraction of image to crop around centre
DISPLAY_SCALE     = 0.35   # scale applied to the cropped region


# ── Robot helpers (identical to eoa_calibrate.py) ────────────────────────────

def robot_connect() -> socket.socket:
    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ROBOT_TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] Cannot connect: {exc}")
        print("  Make sure sort_server.prg is running in RC+ (F5).")
        sys.exit(1)
    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected response: {resp!r}")
        sys.exit(1)
    print(f"  Connected. Robot: {resp}")
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


def _send(s: socket.socket, cmd: str) -> str:
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def robot_move_image(s: socket.socket, rx: float, ry: float) -> None:
    cmd = f"MOVE_IMAGE {rx:.3f},{ry:.3f}"
    print(f"  Robot → {cmd}")
    resp = _send(s, cmd)
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")
    print(f"  At imaging height.")


def robot_ring_on(s: socket.socket) -> None:
    _send(s, "RING_ON")


def robot_ring_off(s: socket.socket) -> None:
    _send(s, "RING_OFF")


def robot_home(s: socket.socket) -> None:
    print("  Robot → HOME")
    _send(s, "HOME")


# ── Camera helpers ────────────────────────────────────────────────────────────

def camera_connect():
    """Connect using the real GigECameraDriver — same code as the main pipeline."""
    import asyncio
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from cib.drivers.camera.driver import GigECameraDriver

    print(f"  Searching for EoA camera (serial {EOA_SERIAL}) ...")
    cam = GigECameraDriver(
        serial=EOA_SERIAL,
        exposure_us=EXPOSURE_US,
        gain_db=GAIN_DB,
        pixel_format="BGR8",
        max_fps=1.0,
        name="eoa_cal",
    )
    asyncio.run(cam.connect())
    print(f"  EoA camera connected.")
    return cam


def capture_frame(cam) -> np.ndarray:
    """Drain 2 stale buffer slots, return a fresh post-settle frame."""
    import asyncio
    asyncio.run(cam.capture())   # drain stale frame 1
    asyncio.run(cam.capture())   # drain stale frame 2
    return asyncio.run(cam.capture())


# ── Interactive click window ──────────────────────────────────────────────────

def click_on_bug(frame: np.ndarray, title: str, instruction: str) -> tuple[float, float]:
    """
    Show a scaled-down EoA image and let the user click on the bug body centre.
    Returns (pixel_x, pixel_y) in FULL-RESOLUTION image coordinates.

    Controls:
      Left-click  — place crosshair
      SPACE/ENTER — confirm
      R           — re-capture (raises RuntimeError so caller can retry)
      Q/ESC       — abort
    """
    h, w = frame.shape[:2]

    # Crop to central region so the bug area fills the window sharply
    cx_full, cy_full = w // 2, h // 2
    crop_hw = int(h * DISPLAY_CROP_FRAC / 2)
    crop_ww = int(w * DISPLAY_CROP_FRAC / 2)
    y0 = cy_full - crop_hw
    y1 = cy_full + crop_hw
    x0 = cx_full - crop_ww
    x1 = cx_full + crop_ww
    crop = frame[y0:y1, x0:x1]

    dw = int(crop.shape[1] * DISPLAY_SCALE)
    dh = int(crop.shape[0] * DISPLAY_SCALE)
    display = cv2.resize(crop, (dw, dh), interpolation=cv2.INTER_LINEAR)
    if display.ndim == 2:
        display = cv2.cvtColor(display, cv2.COLOR_GRAY2BGR)

    click_pos = [None]   # [display_x, display_y] or None

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            click_pos[0] = (x, y)

    win = f"EoA Calibration — {title}"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(win, on_mouse)

    hint = "Left-click: place crosshair  |  SPACE/ENTER: confirm  |  R: recapture  |  Q: abort"

    while True:
        disp = display.copy()

        # Image centre in display coords (centre of crop = centre of full image)
        cx_d, cy_d = dw // 2, dh // 2
        cv2.line(disp, (cx_d - 20, cy_d), (cx_d + 20, cy_d), (0, 200, 0), 1)
        cv2.line(disp, (cx_d, cy_d - 20), (cx_d, cy_d + 20), (0, 200, 0), 1)
        cv2.putText(disp, "cam centre", (cx_d + 5, cy_d - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 200, 0), 1)

        # Draw user click (yellow crosshair)
        if click_pos[0] is not None:
            px, py = click_pos[0]
            arm = 14
            cv2.line(disp, (px - arm, py), (px + arm, py), (0, 255, 255), 2)
            cv2.line(disp, (px, py - arm), (px, py + arm), (0, 255, 255), 2)
            cv2.circle(disp, (px, py), 5, (0, 255, 255), -1)
            dx_d = px - cx_d
            dy_d = py - cy_d
            cv2.putText(disp, f"click offset: ({dx_d:+d}, {dy_d:+d}) disp px",
                        (10, dh - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

        # Instruction banner
        cv2.rectangle(disp, (0, 0), (dw, 50), (20, 20, 20), -1)
        cv2.putText(disp, instruction, (8, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 1)
        cv2.putText(disp, hint, (8, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (160, 160, 160), 1)

        cv2.imshow(win, disp)
        key = cv2.waitKey(30) & 0xFF

        if key in (ord("q"), 27):
            cv2.destroyWindow(win)
            print("\n[ABORT] User quit.")
            sys.exit(0)

        if key in (ord("r"), ord("R")):
            cv2.destroyWindow(win)
            raise RuntimeError("RECAPTURE")

        if key in (ord(" "), 13):   # SPACE or ENTER
            if click_pos[0] is None:
                cv2.putText(disp, "  << click the bug first! >>", (dw // 4, dh // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                cv2.imshow(win, disp)
                cv2.waitKey(800)
                continue
            # Convert display coords → crop coords → full-res coords
            fx = x0 + click_pos[0][0] / DISPLAY_SCALE
            fy = y0 + click_pos[0][1] / DISPLAY_SCALE
            cv2.destroyWindow(win)
            return fx, fy


def capture_and_click(robot, cam, rx: float, ry: float,
                      title: str, instruction: str) -> tuple[float, float]:
    """Move robot, capture EoA frame, get user click. Retries on R key."""
    robot_move_image(robot, rx, ry)
    while True:
        frame = capture_frame(cam)
        try:
            px, py = click_on_bug(frame, title, instruction)
            print(f"  Click confirmed: full-res pixel ({px:.1f}, {py:.1f})")
            return px, py
        except RuntimeError as exc:
            if "RECAPTURE" in str(exc):
                print("  Recapturing...")
                continue
            raise


# ── Calibration maths (same as eoa_calibrate.py) ─────────────────────────────

def compute_calibration(
    center_px: tuple, plus_x_px: tuple, plus_y_px: tuple,
    img_w: int, img_h: int,
    robot_center: tuple, delta_mm: float,
) -> np.ndarray:
    """
    Compute (2,3) calibration array from 3 user-clicked pixel positions.

    center_px   — pixel (x,y) clicked at robot position (rx, ry)
    plus_x_px   — pixel (x,y) clicked at robot position (rx+delta, ry)
    plus_y_px   — pixel (x,y) clicked at robot position (rx, ry+delta)

    Returns numpy (2,3): [M_inv | cam_offset]
    """
    cx0, cy0 = center_px
    cx_x, cy_x = plus_x_px
    cx_y, cy_y = plus_y_px

    # Pixel shifts per +delta_mm robot move
    dpx_dx = cx_x - cx0   # pixel X change for +delta robot X
    dpy_dx = cy_x - cy0   # pixel Y change for +delta robot X
    dpx_dy = cx_y - cx0   # pixel X change for +delta robot Y
    dpy_dy = cy_y - cy0   # pixel Y change for +delta robot Y

    print(f"\n  Pixel shift for +{delta_mm}mm robot X: ({dpx_dx:+.1f}, {dpy_dx:+.1f}) px")
    print(f"  Pixel shift for +{delta_mm}mm robot Y: ({dpx_dy:+.1f}, {dpy_dy:+.1f}) px")

    # M: maps robot-mm-delta → pixel-delta
    # M @ [1, 0]^T = [dpx_dx, dpy_dx] / delta_mm
    # M @ [0, 1]^T = [dpx_dy, dpy_dy] / delta_mm
    M = np.array([
        [dpx_dx / delta_mm, dpx_dy / delta_mm],
        [dpy_dx / delta_mm, dpy_dy / delta_mm],
    ])

    det = np.linalg.det(M)
    print(f"\n  Pixel-per-mm matrix M:\n{M}")
    print(f"  det(M) = {det:.1f}  ({'non-zero = good' if abs(det) > 1 else 'WARNING: near-singular!'})")

    if abs(det) < 1.0:
        print("  [ERROR] Matrix is near-singular — clicks may be on different bugs or too close together.")
        print("  Re-run and click more carefully.")
        sys.exit(1)

    M_inv = np.linalg.inv(M)
    print(f"  M_inv (pixel→robot mm):\n{M_inv}")

    # Camera-to-tool offset: at the center position, the bug is at pixel (cx0, cy0).
    # If the camera were perfectly centred on the tool, the bug would be at (W/2, H/2).
    # The difference is the fixed camera offset.
    pixel_offset_at_center = np.array([cx0 - img_w / 2.0, cy0 - img_h / 2.0])
    cam_offset = M_inv @ pixel_offset_at_center
    print(f"\n  Camera-to-tool offset: ({cam_offset[0]:+.3f}mm, {cam_offset[1]:+.3f}mm)")

    # Pack into (2,3) array
    cal = np.zeros((2, 3), dtype=float)
    cal[:, :2] = M_inv
    cal[:, 2]  = cam_offset
    return cal


def verify_calibration(
    cal: np.ndarray,
    center_px: tuple, plus_x_px: tuple, plus_y_px: tuple,
    img_w: int, img_h: int,
    robot_center: tuple, delta_mm: float,
) -> float:
    """
    Check: given each click pixel, does the calibration predict the correct robot position?
    Returns RMSE in mm.
    """
    M_inv     = cal[:, :2]
    cam_offset = cal[:, 2]
    rx, ry    = robot_center

    positions = [
        (center_px,  rx,            ry,            "center"),
        (plus_x_px,  rx + delta_mm, ry,            "plus_x"),
        (plus_y_px,  rx,            ry + delta_mm, "plus_y"),
    ]

    errors = []
    print("\n  Verification:")
    for (px, py), exp_x, exp_y, label in positions:
        pixel_offset = np.array([px - img_w / 2.0, py - img_h / 2.0])
        # predicted robot delta from center, then add robot_center for absolute
        robot_delta = M_inv @ pixel_offset + cam_offset
        pred_x = rx + robot_delta[0]
        pred_y = ry + robot_delta[1]
        err = np.sqrt((pred_x - exp_x) ** 2 + (pred_y - exp_y) ** 2)
        errors.append(err)
        print(f"    {label:8s}: predicted=({pred_x:.2f}, {pred_y:.2f})  "
              f"expected=({exp_x:.2f}, {exp_y:.2f})  err={err:.3f}mm")

    rmse = float(np.sqrt(np.mean(np.array(errors) ** 2)))
    print(f"\n  RMSE = {rmse:.3f}mm")
    return rmse


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  CIB EoA Camera Calibration  (interactive click version)")
    print("=" * 65)
    print()
    print("BEFORE STARTING:")
    print("  1. sort_server.prg running in RC+ (F5).")
    print("  2. Bugs are on the tray (no special target needed).")
    print("  3. Get overhead XY of one bug from click_to_move.py.")
    print()
    input("  Press ENTER when ready ...")

    # ── Step 1: starting position ─────────────────────────────────────────────
    print()
    print("[STEP 1] Enter the overhead centroid robot XY for one bug.")
    print("         (Copy from click_to_move.py terminal: '[click] pixel ... -> robot (X, Y) mm')")
    print()
    try:
        rx = float(input("  Robot X (mm): ").strip())
        ry = float(input("  Robot Y (mm): ").strip())
    except ValueError:
        print("[ERROR] Invalid number.")
        sys.exit(1)

    print()
    print(f"  Starting position: X={rx:.3f}mm  Y={ry:.3f}mm")
    print(f"  Will visit: center → +{DELTA_MM}mm X → +{DELTA_MM}mm Y")
    print()
    input("  Press ENTER to connect ...")

    # ── Step 2: connect ───────────────────────────────────────────────────────
    print()
    print("[STEP 2] Connecting ...")
    robot  = robot_connect()
    cam = camera_connect()

    print()
    print("[STEP 3] Turning ring light on ...")
    robot_ring_on(robot)
    time.sleep(1.0)   # let LEDs stabilise before first capture

    # ── Step 3: 3 captures with user clicks ──────────────────────────────────
    print()
    print("[STEP 4] Capturing 3 positions — click on the same bug each time ...")
    print()

    img_w, img_h = None, None

    try:
        # CENTER
        print("  --- CENTER ---")
        center_px = capture_and_click(
            robot, cam, rx, ry,
            title="1 of 3 — CENTER",
            instruction=f"Click the bug body centre  (robot at {rx:.1f}, {ry:.1f})",
        )
        # Capture a frame just to get image size
        frame_tmp = capture_frame(cam)
        img_h, img_w = frame_tmp.shape[:2]

        # PLUS X
        print()
        print(f"  --- PLUS X (+{DELTA_MM}mm) ---")
        plus_x_px = capture_and_click(
            robot, cam, rx + DELTA_MM, ry,
            title=f"2 of 3 — PLUS X (+{DELTA_MM}mm)",
            instruction=f"Click the SAME bug  (robot shifted +{DELTA_MM}mm in X)",
        )

        # Back to centre, then PLUS Y
        print()
        print(f"  --- PLUS Y (+{DELTA_MM}mm) ---")
        plus_y_px = capture_and_click(
            robot, cam, rx, ry + DELTA_MM,
            title=f"3 of 3 — PLUS Y (+{DELTA_MM}mm)",
            instruction=f"Click the SAME bug  (robot shifted +{DELTA_MM}mm in Y)",
        )

    finally:
        import asyncio
        try:
            robot_ring_off(robot)
        except Exception:
            pass
        try:
            robot_home(robot)
        except Exception:
            pass
        try:
            _send(robot, "BYE")
        except Exception:
            pass
        robot.close()
        try:
            asyncio.run(cam.disconnect())
        except Exception:
            pass

    print()
    print("[STEP 5] Computing calibration ...")
    print(f"  Image size: {img_w}×{img_h}  centre: ({img_w//2}, {img_h//2})")
    print(f"  center  click: {center_px}")
    print(f"  plus_x  click: {plus_x_px}")
    print(f"  plus_y  click: {plus_y_px}")

    cal = compute_calibration(
        center_px, plus_x_px, plus_y_px,
        img_w, img_h,
        robot_center=(rx, ry),
        delta_mm=DELTA_MM,
    )

    rmse = verify_calibration(
        cal, center_px, plus_x_px, plus_y_px,
        img_w, img_h,
        robot_center=(rx, ry),
        delta_mm=DELTA_MM,
    )

    # ── Step 6: save ─────────────────────────────────────────────────────────
    print()
    print("[STEP 6] Saving calibration ...")
    os.makedirs(OUT_DIR, exist_ok=True)
    np.save(CAL_LOCAL, cal)
    print(f"  Saved: {CAL_LOCAL}")

    runtime_dir = os.path.dirname(CAL_RUNTIME)
    if os.path.isdir(runtime_dir):
        shutil.copy2(CAL_LOCAL, CAL_RUNTIME)
        print(f"  Copied: {CAL_RUNTIME}")
    else:
        print(f"  [WARN] Runtime dir not found ({runtime_dir}) — copy manually.")

    meta = {
        "rmse_mm": round(rmse, 4),
        "delta_mm": DELTA_MM,
        "robot_center_mm": [rx, ry],
        "center_px": list(center_px),
        "plus_x_px": list(plus_x_px),
        "plus_y_px": list(plus_y_px),
        "image_size": [img_w, img_h],
        "M_inv": cal[:, :2].tolist(),
        "cam_offset_mm": cal[:, 2].tolist(),
    }
    meta_path = os.path.join(OUT_DIR, "eoa_calibration_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  Metadata: {meta_path}")

    print()
    print("=" * 65)
    print("  EoA CALIBRATION COMPLETE")
    print()
    print(f"  RMSE          : {rmse:.3f} mm", end="")
    if rmse < 1.0:
        print("  ✓ excellent")
    elif rmse < 3.0:
        print("  ✓ good")
    else:
        print("  ⚠  high — re-run and click more carefully")
    print(f"  Saved to      : {CAL_RUNTIME}")
    print()
    print("  The EoA pipeline will now use this calibration automatically.")
    print("  refined_x / refined_y will appear in logs instead of")
    print("  'no EoA cal yet'.")
    print("=" * 65)


if __name__ == "__main__":
    main()
