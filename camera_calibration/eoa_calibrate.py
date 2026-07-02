"""
eoa_calibrate.py — EoA Camera Calibration
==========================================

Computes the pixel-to-robot-offset transform for the EoA (End-of-Arm) camera.

WHY THIS IS NEEDED
------------------
The overhead camera gives a rough bug centroid (rx, ry) in robot mm.
The robot moves its arm to (rx, ry), then the EoA camera looks at the bug
from close up. The EoA pipeline finds the exact body-centre pixel (bx, by).
But to correct the pick position we need to know:
    "This pixel is Xmm to the right and Ymm below where I went"
This script measures that pixel-to-mm relationship once, saves it to disk,
and the EoA pipeline uses it automatically on every cycle.

WHAT YOU NEED
-------------
  - A calibration target: a small dark filled circle (~8-12mm diameter),
    printed on white paper or cut from black tape. A metal washer works well.
    Place it flat at dish surface level (the same height bugs sit at).
  - sort_server.prg loaded and RUNNING in RC+ (F5).
  - ring light connected (robot digital output 12).
  - EoA camera connected.

HOW IT WORKS
-------------
  1. You jog the robot until the target is visible in the EoA live feed.
  2. You enter the robot XY from the teach pendant.
  3. Script sends MOVE_IMAGE commands to 4 offset positions (±DELTA mm).
  4. At each position it captures an EoA image and detects the target centroid.
  5. From the pixel shifts it computes mm-per-pixel + camera rotation.
  6. Saves calibration_eoa.npy — used by cib/vision/eoa.py automatically.

SAVE FORMAT
-----------
  numpy array shape (2, 3):
    [[m00, m01, cam_offset_x],
     [m10, m11, cam_offset_y]]
  Where M_inv = [[m00, m01], [m10, m11]] maps pixel_offset → robot_offset_mm
  And cam_offset = [cam_offset_x, cam_offset_y] is the fixed camera-to-tool offset.

  At runtime:
    pixel_offset = (body_px - W/2, body_py - H/2)
    robot_offset = M_inv @ pixel_offset + cam_offset
    refined_x    = current_robot_x + robot_offset[0]
    refined_y    = current_robot_y + robot_offset[1]

USAGE
-----
  1. Load sort_server.prg in RC+, press F5 to run.
  2. In a separate terminal, run eoa_live_feed.py to see the camera.
  3. Jog robot over your target until visible in EoA feed (ring light on).
  4. Note the robot XY from the teach pendant.
  5. Run this script:
       C:\\Users\\TejasKumarLeelavathi\\miniconda3\\envs\\cib_env\\python.exe camera_calibration\\eoa_calibrate.py
"""

import ctypes
import json
import os
import shutil
import socket
import sys
import time

import cv2
import numpy as np

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
ROBOT_TIMEOUT = 30.0   # motion commands can take a while at 5% speed

EOA_SERIAL    = "253102169"
EXPOSURE_US   = 24995   # matches machine_config.yaml
GAIN_DB       = 0.0

# Robot moves ±DELTA_MM from starting position to compute pixel scale.
# 15mm gives enough pixel shift without moving too far from the target.
DELTA_MM      = 5.0

# Z heights (mm) — must match sort_server.prg
Z_SAFE        = -47.700
Z_IMAGING     = -58.660

# Output paths
OUT_DIR       = os.path.join(os.path.dirname(__file__), "output")
CAL_LOCAL     = os.path.join(OUT_DIR, "calibration_eoa.npy")
CAL_RUNTIME   = r"C:\LabAutomation\config\calibration_eoa.npy"

# Target detection: we look for the largest dark blob in the EoA image.
# Works for: printed black circle, black tape, metal washer, dark sticker.
MIN_BLOB_AREA_PX = 200    # minimum pixels² — filters out dust
MAX_BLOB_AREA_PX = 50000  # maximum pixels² — filters out background


# ── Robot helpers ─────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket:
    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ROBOT_TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] Cannot connect: {exc}")
        print("  Make sure sort_server.prg is loaded and running in RC+ (F5).")
        sys.exit(1)
    # sort_server.prg waits for the client to speak first — send STATUS, then read.
    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected response from robot: {resp!r}")
        sys.exit(1)
    print(f"  Connected. Robot responded: {resp}")
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
    """Command robot to MOVE_IMAGE position (safe Z → XY → imaging Z)."""
    cmd = f"MOVE_IMAGE {rx:.3f},{ry:.3f}"
    print(f"  Robot → {cmd} ...")
    resp = _send(s, cmd)
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")
    print(f"  Robot at imaging height. ({resp})")


def robot_ring_light(s: socket.socket, on: bool) -> None:
    cmd = "RING_ON" if on else "RING_OFF"
    resp = _send(s, cmd)
    if "ERR" in resp:
        print(f"  [WARN] Ring light command returned: {resp!r}")


def robot_home(s: socket.socket) -> None:
    print("  Robot → HOME ...")
    resp = _send(s, "HOME")
    if "OK HOME" not in resp:
        print(f"  [WARN] HOME returned: {resp!r}")


# ── EoA camera helpers ────────────────────────────────────────────────────────

def camera_connect():
    try:
        from arena_api.system import system
    except ImportError:
        print("[ERROR] arena_api not found. Run with cib_env python.")
        sys.exit(1)

    print(f"  Searching for EoA camera (serial {EOA_SERIAL}) ...")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if any(str(i.get("serial", "")) == EOA_SERIAL for i in infos):
            break
        time.sleep(0.5)

    eoa_info = next(
        (i for i in list(system.device_infos)
         if str(i.get("serial", "")) == EOA_SERIAL), None
    )
    if eoa_info is None:
        print(f"  [ERROR] EoA camera serial {EOA_SERIAL} not found.")
        print("  Run hardware_tests/force_camera_ip.py first.")
        sys.exit(1)

    devices = system.create_device(device_infos=eoa_info)
    device  = devices[0]
    nm = device.nodemap
    try:
        nm["PixelFormat"].value  = "BGR8"
    except Exception:
        pass
    try:
        nm["ExposureAuto"].value = "Off"
        nm["ExposureTime"].value = float(EXPOSURE_US)
    except Exception:
        pass
    try:
        nm["GainAuto"].value = "Off"
        nm["Gain"].value = float(GAIN_DB)
    except Exception:
        pass

    device.start_stream(4)
    print(f"  EoA camera connected. ip={eoa_info.get('ip')}")
    return device, system


def capture_frame(device) -> np.ndarray:
    """Grab a stable frame — drain 2 stale frames first."""
    for _ in range(2):
        buf = device.get_buffer(timeout=5000)
        device.requeue_buffer(buf)

    buf  = device.get_buffer(timeout=5000)
    bpp  = buf.bits_per_pixel // 8
    h, w = buf.height, buf.width
    total = h * w * bpp
    raw  = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
    flat = np.ctypeslib.as_array(raw).copy()
    device.requeue_buffer(buf)

    if bpp == 1:
        return flat.reshape(h, w)
    return flat.reshape(h, w, bpp)


# ── Target detection ──────────────────────────────────────────────────────────

def detect_target(frame: np.ndarray, label: str, save_path: str) -> tuple[float, float]:
    """
    Find the centroid of the calibration target in an EoA frame.

    Target must be a dark blob on a lighter background.
    Returns (pixel_x, pixel_y) of target centroid.
    Saves an annotated debug image to save_path.
    Raises RuntimeError if no suitable blob found.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    h_img, w_img = gray.shape[:2]

    # Only search the central 35% of the image in each axis.
    # The target starts centred and moves ±DELTA_MM — it stays near centre.
    # Other bugs scattered around the frame are excluded completely.
    ROI_FRAC = 0.35
    x0 = int(w_img * (0.5 - ROI_FRAC))
    x1 = int(w_img * (0.5 + ROI_FRAC))
    y0 = int(h_img * (0.5 - ROI_FRAC))
    y1 = int(h_img * (0.5 + ROI_FRAC))
    roi = gray[y0:y1, x0:x1]

    # Otsu inverse threshold on the ROI: dark objects become white
    _, binary_roi = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    # Morphological open to remove noise
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    binary_roi = cv2.morphologyEx(binary_roi, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(binary_roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Offset contour coordinates back to full-image space
    def _offset_contour(c):
        return c + np.array([[[x0, y0]]], dtype=c.dtype)

    # Filter by area and shift contours to full-image coordinates
    candidates = [
        _offset_contour(c) for c in contours
        if MIN_BLOB_AREA_PX <= cv2.contourArea(c) <= MAX_BLOB_AREA_PX
    ]
    if not candidates:
        # Save debug image to help diagnose
        _save_debug(frame, None, None, label, save_path, detected=False)
        raise RuntimeError(
            f"No target blob found in {label} image.\n"
            f"  Image saved: {save_path}\n"
            f"  Check: is the target visible and roughly centred in the EoA view.\n"
            f"  ROI searched: x={x0}-{x1}, y={y0}-{y1} (central 35% of {w_img}x{h_img})\n"
            f"  Adjust MIN_BLOB_AREA_PX / MAX_BLOB_AREA_PX if target is very small/large."
        )

    # Pick the largest blob within the ROI — should be the target
    best = max(candidates, key=cv2.contourArea)
    M   = cv2.moments(best)
    if M["m00"] == 0:
        raise RuntimeError("Blob has zero area moments — detection failed.")

    cx = M["m10"] / M["m00"]
    cy = M["m01"] / M["m00"]

    _save_debug(frame, best, (cx, cy), label, save_path, detected=True)
    print(f"  Target detected at pixel ({cx:.1f}, {cy:.1f})  [{label}]")
    return cx, cy


def _save_debug(
    frame: np.ndarray,
    contour,
    centroid,
    label: str,
    path: str,
    detected: bool,
) -> None:
    """Save annotated debug image so operator can verify detection quality."""
    out = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = out.shape[:2]

    # Image centre crosshair
    cv2.line(out, (w // 2 - 40, h // 2), (w // 2 + 40, h // 2), (0, 255, 0), 2)
    cv2.line(out, (w // 2, h // 2 - 40), (w // 2, h // 2 + 40), (0, 255, 0), 2)
    cv2.putText(out, "IMG CENTRE", (w // 2 + 10, h // 2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    if detected and contour is not None:
        cv2.drawContours(out, [contour], -1, (0, 165, 255), 3)
        bcx, bcy = int(round(centroid[0])), int(round(centroid[1]))
        cv2.circle(out, (bcx, bcy), 10, (0, 0, 255), -1)
        cv2.putText(out, f"TARGET ({centroid[0]:.0f},{centroid[1]:.0f})",
                    (bcx + 15, bcy - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    status = "DETECTED" if detected else "NOT FOUND"
    colour = (0, 200, 0) if detected else (0, 0, 255)
    cv2.putText(out, f"{label} — {status}", (10, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, colour, 3)

    # Scale down to save space (original is 24MP)
    scale = min(1.0, 1280 / w)
    if scale < 1.0:
        out = cv2.resize(out, (int(w * scale), int(h * scale)))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, out)
    print(f"  Debug image saved: {path}")


# ── Calibration math ──────────────────────────────────────────────────────────

def compute_calibration(
    img_w: int, img_h: int,
    p_center: tuple[float, float],
    p_plus_x:  tuple[float, float],
    p_minus_x: tuple[float, float],
    p_plus_y:  tuple[float, float],
    p_minus_y: tuple[float, float],
    delta_mm: float,
) -> np.ndarray:
    """
    Compute the 2x3 EoA calibration matrix from 5 target detections.

    Returns array shape (2, 3):
        [[m00, m01, cam_offset_x],
         [m10, m11, cam_offset_y]]

    Usage at runtime:
        M_inv = cal[:, :2]
        cam_offset = cal[:, 2]
        pixel_off = (body_px - img_w/2, body_py - img_h/2)
        robot_off = M_inv @ pixel_off + cam_offset
        refined_x = current_robot_x + robot_off[0]
        refined_y = current_robot_y + robot_off[1]
    """
    # When robot moves +delta_mm in X:
    #   target pixel shifts by (p_plus_x - p_center) relative to image centre
    # Note: if robot moves right and camera moves right, target appears to move LEFT.
    #   so (p_plus_x - p_center) is typically negative in X.
    # We average +X and -X moves for robustness.

    d_px_from_xmove = (
        (p_plus_x[0] - p_center[0]) - (p_minus_x[0] - p_center[0]) * (-1)
    ) / 2   # average of +X and -X pixel shift (signed)

    # Better: use both directions for cleaner estimate
    # +X move shifts target by (p_plus_x - p_center) pixels in image
    # -X move shifts target by (p_minus_x - p_center) pixels = -(p_plus_x - p_center) ideally
    # Average the magnitudes:
    px_shift_from_robot_x = np.array([
        (p_plus_x[0] - p_center[0]),
        (p_plus_x[1] - p_center[1]),
    ])
    px_shift_from_robot_y = np.array([
        (p_plus_y[0] - p_center[0]),
        (p_plus_y[1] - p_center[1]),
    ])

    # Use ±delta pairs for better accuracy
    px_shift_from_robot_x_avg = np.array([
        (p_plus_x[0] - p_minus_x[0]) / 2,
        (p_plus_x[1] - p_minus_x[1]) / 2,
    ])
    px_shift_from_robot_y_avg = np.array([
        (p_plus_y[0] - p_minus_y[0]) / 2,
        (p_plus_y[1] - p_minus_y[1]) / 2,
    ])

    # Matrix M maps robot_delta_mm → pixel_delta
    # Column 0: pixel shift per mm of robot X
    # Column 1: pixel shift per mm of robot Y
    # M = [px_shift_from_robot_x_avg / delta_mm  |  px_shift_from_robot_y_avg / delta_mm]
    M = np.column_stack([
        px_shift_from_robot_x_avg / delta_mm,
        px_shift_from_robot_y_avg / delta_mm,
    ])   # shape (2, 2)

    print(f"\n  Pixel-per-mm matrix M:\n{M}")
    print(f"  det(M) = {np.linalg.det(M):.4f}  (non-zero = good)")

    if abs(np.linalg.det(M)) < 0.01:
        raise RuntimeError(
            "Calibration matrix is near-singular — robot moves may be too small "
            "or target detection is unreliable. Try a larger DELTA_MM."
        )

    M_inv = np.linalg.inv(M)   # maps pixel_delta → robot_delta_mm
    print(f"  M_inv (pixel→robot mm):\n{M_inv}")

    # Camera-to-tool offset:
    # When robot is at starting position R0, the target is at p_center in the image.
    # If camera were exactly on the tool, p_center would be (W/2, H/2).
    # The offset of p_center from image centre gives the fixed camera-tool offset.
    img_cx = img_w / 2.0
    img_cy = img_h / 2.0
    pixel_offset_at_R0 = np.array([p_center[0] - img_cx, p_center[1] - img_cy])
    cam_offset = M_inv @ pixel_offset_at_R0   # robot mm offset

    print(f"\n  Camera-to-tool offset: ({cam_offset[0]:+.2f}mm, {cam_offset[1]:+.2f}mm)")
    print(f"  (how far camera optical centre is from robot tool position)")

    # Build 2x3 calibration matrix
    cal = np.zeros((2, 3), dtype=np.float64)
    cal[:, :2] = M_inv
    cal[:, 2]  = cam_offset

    return cal


def compute_rmse(
    cal: np.ndarray, img_w: int, img_h: int,
    measurements: list[tuple],
) -> float:
    """
    Verify calibration accuracy.
    measurements: list of (target_px, target_py, robot_x, robot_y, ref_robot_x, ref_robot_y)
    where ref = where we expect the body to be.
    """
    M_inv     = cal[:, :2]
    cam_offset = cal[:, 2]
    img_cx, img_cy = img_w / 2.0, img_h / 2.0

    errors = []
    for (tpx, tpy, rx, ry, exp_x, exp_y) in measurements:
        pixel_off = np.array([tpx - img_cx, tpy - img_cy])
        robot_off = M_inv @ pixel_off + cam_offset
        pred_x = rx + robot_off[0]
        pred_y = ry + robot_off[1]
        err = np.sqrt((pred_x - exp_x) ** 2 + (pred_y - exp_y) ** 2)
        errors.append(err)
        print(f"  Position ({rx:+.1f},{ry:+.1f}): predicted=({pred_x:.2f},{pred_y:.2f})  "
              f"expected=({exp_x:.2f},{exp_y:.2f})  err={err:.3f}mm")

    rmse = float(np.sqrt(np.mean(np.array(errors) ** 2)))
    print(f"\n  RMSE = {rmse:.3f}mm")
    return rmse


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 65)
    print("  CIB EoA Camera Calibration")
    print("=" * 65)
    print("""
BEFORE STARTING:
  1. Place a small dark circular target (~8-12mm) at dish surface
     level in the sorting cell. A black dot sticker, tape circle,
     or metal washer all work well.
  2. Load sort_server.prg in RC+ and press F5 to run it.
  3. Turn ring light on (robot output 12 — or run the script and
     let it command RING_ON via the robot).
  4. In another terminal, run:
       eoa_live_feed.py
     Jog the robot until the target is visible and roughly centred
     in the EoA camera view.
  5. Note the robot XY coordinates from the teach pendant.
  6. Press ENTER here to continue.
    """)
    input("  Press ENTER when ready ...")

    # ── Get starting position from operator ────────────────────────────────────
    print("\n[STEP 1] Enter starting robot XY (read from teach pendant).")
    print("         This is where the target is visible in the EoA view.")
    while True:
        try:
            rx0 = float(input("  Robot X (mm): ").strip())
            ry0 = float(input("  Robot Y (mm): ").strip())
            break
        except ValueError:
            print("  Please enter numbers only.")

    print(f"\n  Starting position: X={rx0:.3f}mm  Y={ry0:.3f}mm")
    print(f"  Will move ±{DELTA_MM}mm in X and Y from this position.")

    # Positions to visit: centre, +X, -X, +Y, -Y
    positions = {
        "center":  (rx0,              ry0),
        "plus_x":  (rx0 + DELTA_MM,   ry0),
        "minus_x": (rx0 - DELTA_MM,   ry0),
        "plus_y":  (rx0,              ry0 + DELTA_MM),
        "minus_y": (rx0,              ry0 - DELTA_MM),
    }

    print("\n  Positions to visit:")
    for name, (rx, ry) in positions.items():
        print(f"    {name:<10} → X={rx:+.1f}  Y={ry:+.1f}")

    input("\n  Press ENTER to connect to robot and camera ...")

    # ── Connect ────────────────────────────────────────────────────────────────
    print("\n[STEP 2] Connecting ...")
    robot = robot_connect()
    device, system = camera_connect()

    img_w, img_h = None, None
    detections   = {}   # name → (pixel_x, pixel_y)

    os.makedirs(OUT_DIR, exist_ok=True)
    debug_dir = os.path.join(OUT_DIR, "eoa_cal_debug")
    os.makedirs(debug_dir, exist_ok=True)

    try:
        # ── Ring light on ──────────────────────────────────────────────────────
        print("\n[STEP 3] Turning ring light on ...")
        robot_ring_light(robot, True)

        # ── Visit each position, capture, detect ───────────────────────────────
        print(f"\n[STEP 4] Visiting {len(positions)} positions ...")
        for name, (rx, ry) in positions.items():
            print(f"\n  --- {name.upper()} (X={rx:+.1f}, Y={ry:+.1f}) ---")
            robot_move_image(robot, rx, ry)
            time.sleep(0.5)   # let vibration settle

            frame = capture_frame(device)
            if img_w is None:
                img_h, img_w = frame.shape[:2]
                print(f"  Image size: {img_w}×{img_h}px")

            debug_path = os.path.join(debug_dir, f"eoa_cal_{name}.jpg")
            px, py = detect_target(frame, name, debug_path)
            detections[name] = (px, py)

        # ── Ring light off, go home ────────────────────────────────────────────
        robot_ring_light(robot, False)
        robot_home(robot)

    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
        robot_ring_light(robot, False)
        try:
            robot_home(robot)
        except Exception:
            pass
        device.stop_stream()
        system.destroy_device()
        sys.exit(1)
    finally:
        device.stop_stream()
        system.destroy_device()

    # ── Compute calibration ────────────────────────────────────────────────────
    print("\n[STEP 5] Computing calibration ...")
    print(f"  Image size: {img_w}×{img_h}  Image centre: ({img_w/2:.0f}, {img_h/2:.0f})")
    print(f"\n  Target pixel detections:")
    for name, (px, py) in detections.items():
        print(f"    {name:<10} → ({px:.1f}, {py:.1f})")

    cal = compute_calibration(
        img_w, img_h,
        p_center  = detections["center"],
        p_plus_x  = detections["plus_x"],
        p_minus_x = detections["minus_x"],
        p_plus_y  = detections["plus_y"],
        p_minus_y = detections["minus_y"],
        delta_mm  = DELTA_MM,
    )

    # ── Verify: target is at a known robot position ────────────────────────────
    # The target's real robot position is the starting position R0 (where it was
    # centred). Check that each measurement predicts this correctly.
    print("\n[STEP 6] Verifying accuracy ...")
    target_real_x = rx0
    target_real_y = ry0

    measurements = [
        (detections["center"][0], detections["center"][1], rx0,           ry0,           target_real_x, target_real_y),
        (detections["plus_x"][0], detections["plus_x"][1], rx0+DELTA_MM,  ry0,           target_real_x, target_real_y),
        (detections["minus_x"][0],detections["minus_x"][1],rx0-DELTA_MM,  ry0,           target_real_x, target_real_y),
        (detections["plus_y"][0], detections["plus_y"][1], rx0,           ry0+DELTA_MM,  target_real_x, target_real_y),
        (detections["minus_y"][0],detections["minus_y"][1],rx0,           ry0-DELTA_MM,  target_real_x, target_real_y),
    ]
    rmse = compute_rmse(cal, img_w, img_h, measurements)

    if rmse > 1.0:
        print(f"\n  [WARN] RMSE={rmse:.3f}mm is high (>1mm).")
        print("  Possible causes:")
        print("  - Target moved during robot moves (not fixed to surface)")
        print("  - Poor target detection (check debug images in output/eoa_cal_debug/)")
        print("  - Robot not returning to exact positions (check RC+ accuracy mode)")
    else:
        print(f"\n  [PASS] RMSE={rmse:.3f}mm — calibration looks good.")

    # ── Save ──────────────────────────────────────────────────────────────────
    print(f"\n[STEP 7] Saving calibration ...")
    np.save(CAL_LOCAL, cal)
    print(f"  Saved: {CAL_LOCAL}")

    os.makedirs(os.path.dirname(CAL_RUNTIME), exist_ok=True)
    shutil.copy2(CAL_LOCAL, CAL_RUNTIME)
    print(f"  Copied: {CAL_RUNTIME}")

    # Save metadata JSON for reference
    meta = {
        "img_width":   img_w,
        "img_height":  img_h,
        "delta_mm":    DELTA_MM,
        "rmse_mm":     round(rmse, 4),
        "M_inv":       cal[:, :2].tolist(),
        "cam_offset":  cal[:, 2].tolist(),
        "detections":  {k: list(v) for k, v in detections.items()},
        "starting_xy": [rx0, ry0],
    }
    meta_path = os.path.join(OUT_DIR, "eoa_calibration_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  Metadata: {meta_path}")

    print(f"""
{'=' * 65}
  EoA CALIBRATION COMPLETE

  RMSE          : {rmse:.3f} mm
  Saved to      : {CAL_RUNTIME}
  Debug images  : {debug_dir}\\

  The EoA pipeline will now use this calibration automatically.
  refined_x / refined_y will be populated in the logs instead of
  showing "no EoA cal yet".

  To re-run calibration (e.g. if camera is re-mounted):
    Delete {CAL_RUNTIME}
    Run this script again.
{'=' * 65}
""")


if __name__ == "__main__":
    main()
