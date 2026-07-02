"""
eoa_scale_calibrate.py — EoA Camera Pixel Scale Calibration
============================================================

Uses a physical checkerboard (20mm squares) to measure how many pixels
per mm the EoA camera sees at dish surface height.

This is a PREREQUISITE to running eoa_calibrate.py.  It measures pixel
scale + camera rotation angle — purely from image geometry, no robot
motion needed.

WHAT YOU NEED
-------------
  - Checkerboard with 20mm squares stuck flat on the dish tray.
    At least 2 complete squares visible in each direction (3×3 squares
    or larger works best — even a 2×3 arrangement is fine).
  - sort_server.prg loaded and RUNNING in RC+ (for ring light + MOVE_IMAGE).
    If you don't have the robot: set ROBOT_REQUIRED = False below and turn
    the ring light on manually.
  - EoA camera connected.

HOW IT WORKS
------------
  1. Robot moves to imaging height above the checkerboard.
  2. Ring light turns on.
  3. A live EoA feed opens.  The script tries every plausible inner-corner
     count (e.g. 3×3, 4×3, 2×3 …) on each frame.
  4. When corners are found, they are drawn on the live view.
  5. Press SPACE to accept the current detection and save the result.
  6. Press R to retry detection on a fresh frame.
  7. Press Q to quit without saving.

OUTPUT
------
  camera_calibration/output/eoa_scale.json:
    {
      "px_per_mm":        12.34,
      "rotation_deg":     2.1,
      "inner_cols":       3,
      "inner_rows":       3,
      "mean_h_px":        246.8,
      "mean_v_px":        246.5,
      "img_width":        2448,
      "img_height":       2048,
      "square_size_mm":   20.0
    }

USAGE
-----
  python hardware_tests/eoa_scale_calibrate.py
"""

import ctypes
import json
import os
import socket
import sys
import time

import cv2
import numpy as np

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
ROBOT_TIMEOUT = 30.0

# Set False to skip robot connection (turn ring light on manually).
ROBOT_REQUIRED = True

EOA_SERIAL    = "253102169"
EXPOSURE_US   = 24995   # matches machine_config.yaml
GAIN_DB       = 0.0

SQUARE_SIZE_MM = 20.0   # physical size of each checkerboard square

# Output paths
CAL_DIR  = os.path.join(os.path.dirname(__file__), "..", "camera_calibration", "output")
OUT_JSON = os.path.join(CAL_DIR, "eoa_scale.json")
OUT_IMG  = os.path.join(CAL_DIR, "eoa_scale_debug.jpg")

# Inner-corner patterns to try, ordered largest→smallest.
# Inner corners = (squares_wide - 1) × (squares_tall - 1).
# E.g. a 4×3 square grid has 3×2 = (3,2) inner corners.
# Try both orientations (cols, rows) and (rows, cols) automatically.
PATTERNS_TO_TRY = [
    (7, 6), (6, 7), (6, 5), (5, 6),
    (5, 4), (4, 5), (5, 5), (5, 3), (3, 5),
    (4, 4), (4, 3), (3, 4),
    (3, 3), (4, 2), (2, 4),
    (3, 2), (2, 3),
    (2, 2),
]

# ── Robot helpers ─────────────────────────────────────────────────────────────

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


def robot_connect() -> socket.socket | None:
    if not ROBOT_REQUIRED:
        print("  [INFO] ROBOT_REQUIRED=False — skipping robot connection.")
        print("         Turn ring light on manually before continuing.")
        return None

    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ROBOT_TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] Cannot connect to robot: {exc}")
        print("  Make sure sort_server.prg is loaded and running in RC+.")
        if ROBOT_REQUIRED:
            sys.exit(1)
        return None

    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected robot response: {resp!r}")
        sys.exit(1)
    print(f"  Robot connected: {resp}")
    return s


def robot_move_image(s: socket.socket, rx: float, ry: float) -> None:
    cmd = f"MOVE_IMAGE {rx:.3f},{ry:.3f}"
    print(f"  Robot → {cmd} ...")
    resp = _send(s, cmd)
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")
    print(f"  Robot at imaging height.")


def robot_ring_light(s: socket.socket, on: bool) -> None:
    resp = _send(s, "RING_ON" if on else "RING_OFF")
    if "ERR" in resp:
        print(f"  [WARN] Ring light: {resp!r}")


def robot_home(s: socket.socket) -> None:
    print("  Robot → HOME ...")
    _send(s, "HOME")


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
    """Drain 2 stale frames, then grab one fresh frame."""
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


# ── Checkerboard detection ────────────────────────────────────────────────────

# Detect on a downsampled image — much faster, then scale corners back up.
# 1280px wide is plenty for checkerboard corner detection.
DETECT_MAX_WIDTH = 1280


def _refine_corners(gray: np.ndarray, corners: np.ndarray) -> np.ndarray:
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 0.0001)
    return cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)


def detect_corners(frame: np.ndarray):
    """
    Try to find checkerboard inner corners automatically.

    Downsamples to DETECT_MAX_WIDTH before detection so it runs in seconds
    even on a 24MP image.  Corner coordinates are scaled back to full-res.

    Returns (corners_2d, cols, rows, px_per_mm, rotation_deg, h_px, v_px)
    where all pixel values are in full-resolution coordinates.
    Returns None if no pattern found.
    """
    h_full, w_full = frame.shape[:2]
    ds = min(1.0, DETECT_MAX_WIDTH / w_full)   # downsample factor

    # Work on a small grayscale copy for detection
    small = cv2.resize(frame, (int(w_full * ds), int(h_full * ds)),
                       interpolation=cv2.INTER_AREA)
    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small.copy()
    gray  = cv2.equalizeHist(gray)

    print(f"  Detecting on {gray.shape[1]}×{gray.shape[0]} (downsampled {ds:.2f}×) ...")

    cb_flags = (
        cv2.CALIB_CB_ADAPTIVE_THRESH |
        cv2.CALIB_CB_NORMALIZE_IMAGE |
        cv2.CALIB_CB_FAST_CHECK
    )

    for cols, rows in PATTERNS_TO_TRY:
        found   = False
        corners = None

        # findChessboardCornersSB requires both dims > 2 (OpenCV 4.1+)
        if hasattr(cv2, "findChessboardCornersSB") and cols > 2 and rows > 2:
            sb_flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
            ret, corners = cv2.findChessboardCornersSB(gray, (cols, rows), sb_flags)
            if ret:
                found = True

        if not found:
            ret, corners = cv2.findChessboardCorners(gray, (cols, rows), cb_flags)
            if ret:
                corners = _refine_corners(gray, corners)
                found   = True

        if not found:
            continue

        # Scale corners back to full-resolution coordinates
        corners_full = corners / ds   # shape: (cols*rows, 1, 2)

        # Refine at full resolution for best sub-pixel accuracy
        gray_full = (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                     if frame.ndim == 3 else frame.copy())
        corners_full = _refine_corners(gray_full, corners_full.astype(np.float32))

        # Reshape to (rows, cols, 2)
        corners_2d = corners_full.reshape(rows, cols, 2)

        # ── Mean horizontal spacing (along rows) ──────────────────────────────
        h_dists = []
        for r in range(rows):
            for c in range(cols - 1):
                d = float(np.linalg.norm(corners_2d[r, c + 1] - corners_2d[r, c]))
                h_dists.append(d)

        # ── Mean vertical spacing (along columns) ─────────────────────────────
        v_dists = []
        for r in range(rows - 1):
            for c in range(cols):
                d = float(np.linalg.norm(corners_2d[r + 1, c] - corners_2d[r, c]))
                v_dists.append(d)

        if not h_dists or not v_dists:
            continue

        mean_h_px = float(np.mean(h_dists))
        mean_v_px = float(np.mean(v_dists))

        # Sanity: h and v spacing should match (square board).  >40% diff = wrong.
        ratio = mean_h_px / mean_v_px if mean_v_px > 0 else 0
        if not (0.6 <= ratio <= 1.4):
            print(f"  [SKIP] ({cols}×{rows}) h={mean_h_px:.1f} v={mean_v_px:.1f} "
                  f"ratio={ratio:.2f} — skipping")
            continue

        mean_px      = (mean_h_px + mean_v_px) / 2.0
        px_per_mm    = mean_px / SQUARE_SIZE_MM

        # Rotation: direction of first row of corners
        p0 = corners_2d[0, 0]
        p1 = corners_2d[0, -1]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        rotation_deg = float(np.degrees(np.arctan2(dy, dx)))

        print(f"  [FOUND] pattern ({cols}×{rows})  "
              f"h={mean_h_px:.1f}px  v={mean_v_px:.1f}px  "
              f"scale={px_per_mm:.3f}px/mm  rot={rotation_deg:+.2f}°")

        return corners_2d, cols, rows, px_per_mm, rotation_deg, mean_h_px, mean_v_px

    return None


# ── Annotation helpers ────────────────────────────────────────────────────────

def annotate_frame(frame: np.ndarray, result, scale_factor: float = 1.0) -> np.ndarray:
    """Draw corner detections and scale info on a copy of frame."""
    vis = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = vis.shape[:2]

    # Image centre crosshair
    cx, cy = w // 2, h // 2
    cv2.line(vis, (cx - 60, cy), (cx + 60, cy), (0, 255, 0), 2)
    cv2.line(vis, (cx, cy - 60), (cx, cy + 60), (0, 255, 0), 2)

    if result is None:
        cv2.putText(vis, "NO PATTERN FOUND", (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 4)
        cv2.putText(vis, "Check: ring light on, checkerboard flat and centred",
                    (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        cv2.putText(vis, "SPACE=retry  Q=quit", (20, h - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
    else:
        corners_2d, cols, rows, px_per_mm, rotation_deg, mean_h_px, mean_v_px = result

        # Draw corners
        corners_flat = corners_2d.reshape(-1, 1, 2).astype(np.float32)
        cv2.drawChessboardCorners(vis, (cols, rows), corners_flat, True)

        # Draw lines along first and last row to show rotation clearly
        def pt(p):
            return (int(round(p[0])), int(round(p[1])))

        for r in range(rows):
            cv2.line(vis, pt(corners_2d[r, 0]), pt(corners_2d[r, -1]), (255, 0, 255), 2)
        for c in range(cols):
            cv2.line(vis, pt(corners_2d[0, c]), pt(corners_2d[-1, c]), (255, 0, 255), 2)

        # Label each corner with its index
        for r in range(rows):
            for c in range(cols):
                p = pt(corners_2d[r, c])
                cv2.circle(vis, p, 6, (0, 255, 255), -1)

        # Stats overlay
        lines = [
            f"Pattern: {cols+1}x{rows+1} squares ({cols}x{rows} inner corners)",
            f"Scale:   {px_per_mm:.3f} px/mm  ({1000/px_per_mm:.2f} um/px)",
            f"H-step:  {mean_h_px:.1f} px  V-step: {mean_v_px:.1f} px",
            f"Rotation: {rotation_deg:+.2f} deg",
        ]
        y0 = 60
        for i, line in enumerate(lines):
            cv2.putText(vis, line, (20, y0 + i * 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

        cv2.putText(vis, "SPACE=save  R=refresh  Q=quit",
                    (20, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)

    # Scale down for display (original is up to 24MP)
    if scale_factor < 1.0:
        new_w = int(w * scale_factor)
        new_h = int(h * scale_factor)
        vis = cv2.resize(vis, (new_w, new_h), interpolation=cv2.INTER_AREA)

    return vis


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 65)
    print("  CIB EoA Camera — Pixel Scale Calibration")
    print("  (checkerboard method, 20mm squares)")
    print("=" * 65)
    print("""
BEFORE STARTING:
  1. Stick checkerboard (20mm squares) flat on the dish tray.
     At least 2 full squares visible in each direction.
  2. Load sort_server.prg in RC+ and press F5 to run it.
  3. Jog the robot arm over the checkerboard in RC+.
  4. Note the robot XY (teach pendant) and enter it below.
""")

    # ── Get robot XY ──────────────────────────────────────────────────────────
    robot = None
    rx0, ry0 = 0.0, 0.0

    if ROBOT_REQUIRED:
        while True:
            try:
                rx0 = float(input("  Robot X over checkerboard (mm): ").strip())
                ry0 = float(input("  Robot Y over checkerboard (mm): ").strip())
                break
            except ValueError:
                print("  Numbers only please.")
        print(f"\n  Will move robot to imaging height at X={rx0:.1f} Y={ry0:.1f}.")
        input("  Press ENTER to connect ...")
    else:
        print("  [INFO] Robot skipped. Turn ring light on manually.")
        input("  Press ENTER to connect to camera ...")

    # ── Connect ───────────────────────────────────────────────────────────────
    print()
    robot = robot_connect()
    device, arena_system = camera_connect()

    try:
        if robot is not None:
            print("\n  Moving robot to imaging height ...")
            robot_move_image(robot, rx0, ry0)
            time.sleep(0.3)
            print("  Ring light ON ...")
            robot_ring_light(robot, True)
            time.sleep(0.2)

        # ── Capture first frame ───────────────────────────────────────────────
        print("\n  Capturing frame ...")
        frame = capture_frame(device)
        h_img, w_img = frame.shape[:2]
        print(f"  Image size: {w_img}×{h_img}")

        # Display scale factor (fit into ~1280px wide window)
        sf = min(1.0, 1280.0 / w_img)

        # ── Interactive live loop ─────────────────────────────────────────────
        WIN = "EoA Scale Calibration"
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WIN, int(w_img * sf), int(h_img * sf))

        result      = None
        saved       = False
        last_result = None

        print("\n  Live view open.")
        print("  Keys: SPACE = save result | R = refresh frame | Q = quit\n")

        while True:
            if result is None:
                print("  Searching for checkerboard corners ...")
                try:
                    result = detect_corners(frame)
                except Exception as exc:
                    print(f"  [ERROR] Detection failed: {exc}")
                    result = None
                if result is None:
                    print("  [WARN] No pattern found. Check lighting / positioning.")
                    print("         Press R to capture a fresh frame, Q to quit.")

            vis = annotate_frame(frame, result, scale_factor=sf)
            cv2.imshow(WIN, vis)
            key = cv2.waitKey(0) & 0xFF

            if key in (ord('q'), ord('Q'), 27):  # Q or Esc
                print("  Quit — no result saved.")
                break

            elif key in (ord('r'), ord('R'), ord(' ')):
                # R = refresh frame and re-detect
                # SPACE = save (but only if we have a good result)
                if key in (ord('r'), ord('R')) or result is None:
                    print("  Refreshing frame ...")
                    frame  = capture_frame(device)
                    result = None
                    continue

                # SPACE with valid result → save
                corners_2d, cols, rows, px_per_mm, rotation_deg, mean_h_px, mean_v_px = result

                # ── Save annotated debug image ─────────────────────────────────
                os.makedirs(CAL_DIR, exist_ok=True)
                # Save full-res annotated image
                vis_full = annotate_frame(frame, result, scale_factor=1.0)
                cv2.imwrite(OUT_IMG, vis_full)
                print(f"\n  Debug image saved: {OUT_IMG}")

                # ── Save JSON ──────────────────────────────────────────────────
                out = {
                    "px_per_mm":      round(px_per_mm, 6),
                    "rotation_deg":   round(rotation_deg, 4),
                    "inner_cols":     cols,
                    "inner_rows":     rows,
                    "mean_h_px":      round(mean_h_px, 4),
                    "mean_v_px":      round(mean_v_px, 4),
                    "img_width":      w_img,
                    "img_height":     h_img,
                    "square_size_mm": SQUARE_SIZE_MM,
                }
                with open(OUT_JSON, "w") as f:
                    json.dump(out, f, indent=2)
                print(f"  Scale saved:       {OUT_JSON}")

                print(f"""
{'=' * 65}
  RESULT

  Pixel scale   : {px_per_mm:.3f} px / mm
  Resolution    : {1000/px_per_mm:.2f} µm / px
  Rotation      : {rotation_deg:+.2f}°
  Pattern used  : {cols+1}×{rows+1} squares ({cols}×{rows} inner corners)
  H-step        : {mean_h_px:.1f} px  V-step: {mean_v_px:.1f} px
  Image size    : {w_img}×{h_img} px
{'=' * 65}
""")
                saved = True
                break

        cv2.destroyAllWindows()

    finally:
        if robot is not None:
            robot_ring_light(robot, False)
            robot_home(robot)
        device.stop_stream()
        arena_system.destroy_device()

    if not saved:
        print("  No calibration saved.")
    else:
        print("  Done. Load this scale in your EoA pipeline via eoa_scale.json.")


if __name__ == "__main__":
    main()
