"""
eoa_center_bug.py — EoA bug centering using pixel-scale calibration
====================================================================

Flow:
  1. Overhead snapshot → vision → robot moves to bug (rough position)
  2. EoA captures frame → detects bug pixel offset from image centre
  3. Converts offset (px) → mm using eoa_pixel_scale.json
  4. Robot moves by that correction → bug is now under the EoA crosshair
  5. Shows final EoA frame

Usage:
  python hardware_tests/eoa_center_bug.py
"""

import ctypes
import json
import os
import socket
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
ROBOT_TIMEOUT = 60.0

OVERHEAD_SERIAL  = "253102168"
EOA_SERIAL       = "253102169"

EOA_EXPOSURE_US      = 24995
EOA_GAIN_DB          = 0.0
OVERHEAD_EXPOSURE_US = 10000
OVERHEAD_GAIN_DB     = 0.0

BLOB_THRESHOLD = 100
MIN_BLOB_AREA  = 300
MAX_BLOB_AREA  = 80000

DISPLAY_WIDTH = 1280

CAL_JSON = os.path.join(
    os.path.dirname(__file__), "..", "camera_calibration", "output", "eoa_pixel_scale.json"
)


# ── Robot ─────────────────────────────────────────────────────────────────────

def _recv_line(s):
    buf = b""
    while True:
        ch = s.recv(1)
        if not ch:
            raise ConnectionError("Robot disconnected.")
        buf += ch
        if ch == b"\n":
            break
    return buf.decode("ascii", errors="replace").strip()


def _send(s, cmd):
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def robot_connect():
    print(f"  Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ROBOT_TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"  [ERROR] {exc}")
        sys.exit(1)
    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected robot response: {resp!r}")
        sys.exit(1)
    print(f"  Robot ready: {resp}")
    return s


def robot_move(s, rx, ry):
    cmd = f"MOVE_IMAGE {rx:.4f},{ry:.4f}"
    print(f"  Robot → {cmd}")
    resp = _send(s, cmd)
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")


def robot_ring_on(s):
    _send(s, "RING_ON")


def robot_ring_off(s):
    try:
        _send(s, "RING_OFF")
    except Exception:
        pass


def robot_home(s):
    print("  Robot → HOME ...")
    try:
        _send(s, "HOME")
    except Exception as exc:
        print(f"  [WARN] HOME failed: {exc}")


# ── Camera ────────────────────────────────────────────────────────────────────

def camera_open(serial, exposure_us, gain_db):
    from arena_api.system import system
    print(f"  Waiting for camera {serial} ...")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(str(i.get("serial", "")) == serial for i in list(system.device_infos)):
            break
        time.sleep(0.5)
    info = next((i for i in list(system.device_infos)
                 if str(i.get("serial", "")) == serial), None)
    if info is None:
        print(f"  [ERROR] Camera {serial} not found.")
        sys.exit(1)
    dev = system.create_device(device_infos=info)[0]
    nm  = dev.nodemap
    for k, v in [("PixelFormat", "BGR8"), ("ExposureAuto", "Off"),
                  ("ExposureTime", float(exposure_us)), ("GainAuto", "Off"),
                  ("Gain", float(gain_db))]:
        try:
            nm[k].value = v
        except Exception:
            pass
    dev.start_stream(4)
    print(f"  Camera {serial} open.")
    return dev, system


def camera_close(dev, sys_obj):
    try:
        dev.stop_stream()
    except Exception:
        pass
    try:
        sys_obj.destroy_device()
    except Exception:
        pass


def grab_frame(dev):
    for _ in range(2):
        b = dev.get_buffer(timeout=5000)
        dev.requeue_buffer(b)
    b   = dev.get_buffer(timeout=5000)
    bpp = b.bits_per_pixel // 8
    h, w = b.height, b.width
    raw = (ctypes.c_uint8 * (h * w * bpp)).from_address(ctypes.addressof(b.pbytes))
    arr = np.ctypeslib.as_array(raw).copy()
    dev.requeue_buffer(b)
    return arr.reshape(h, w, bpp) if bpp > 1 else arr.reshape(h, w)


# ── Detection ─────────────────────────────────────────────────────────────────

def find_bug(frame, threshold=BLOB_THRESHOLD):
    """Closest dark blob to image centre. Returns (cx, cy) or None."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame.copy()
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)
    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k, iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid = [c for c in contours if MIN_BLOB_AREA < cv2.contourArea(c) < MAX_BLOB_AREA]
    if not valid:
        return None
    img_cx, img_cy = frame.shape[1] / 2.0, frame.shape[0] / 2.0
    best = None
    best_dist = float("inf")
    for c in valid:
        M = cv2.moments(c)
        if M["m00"] == 0:
            continue
        cx = M["m10"] / M["m00"]
        cy = M["m01"] / M["m00"]
        d  = (cx - img_cx) ** 2 + (cy - img_cy) ** 2
        if d < best_dist:
            best_dist = d
            best = (cx, cy)
    return best


def detect_bug(dev, label):
    """Capture with retry on relaxed threshold."""
    t = BLOB_THRESHOLD
    for attempt in range(3):
        frame = grab_frame(dev)
        bug   = find_bug(frame, threshold=t)
        if bug is not None:
            if attempt > 0:
                print(f"  [{label}] detected on retry {attempt} (threshold={t})")
            return bug, frame
        t = min(t + 20, 200)
        print(f"  [{label}] retrying with threshold={t} ...")
        time.sleep(0.3)
    raise RuntimeError(f"Bug not detected at {label}.")


# ── Display ───────────────────────────────────────────────────────────────────

def show_frame(frame, bug, title, info_lines=None):
    H, W = frame.shape[:2]
    cx, cy = W // 2, H // 2
    vis = frame.copy()

    # Bug — RED
    bx, by = int(bug[0]), int(bug[1])
    cv2.circle(vis, (bx, by), 20, (0, 0, 255), 3)
    cv2.line(vis, (bx-30, by), (bx+30, by), (0, 0, 255), 2)
    cv2.line(vis, (bx, by-30), (bx, by+30), (0, 0, 255), 2)

    # Crosshair — GREEN
    cv2.line(vis, (cx-300, cy), (cx+300, cy), (0, 255, 0), 5)
    cv2.line(vis, (cx, cy-300), (cx, cy+300), (0, 255, 0), 5)
    cv2.circle(vis, (cx, cy), 40, (0, 255, 0), 4)

    # Offset line — ORANGE
    cv2.line(vis, (cx, cy), (bx, by), (0, 165, 255), 2)

    offset_px = np.sqrt((bx-cx)**2 + (by-cy)**2)
    cv2.putText(vis, f"Offset: ({bx-cx:+d}, {by-cy:+d}) px  |{offset_px:.0f}| px",
                (20, H-80), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)

    if info_lines:
        for i, line in enumerate(info_lines):
            cv2.putText(vis, line, (20, 60 + i*60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 0), 3)

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    sf = DISPLAY_WIDTH / W
    cv2.resizeWindow(title, DISPLAY_WIDTH, int(H * sf))
    disp = cv2.resize(vis, (DISPLAY_WIDTH, int(H * sf)), interpolation=cv2.INTER_AREA)
    cv2.imshow(title, disp)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  EoA Bug Centering")
    print("=" * 65)

    # Load calibration
    if not os.path.exists(CAL_JSON):
        print(f"  [ERROR] {CAL_JSON} not found. Run eoa_pixel_scale.py first.")
        sys.exit(1)
    with open(CAL_JSON) as f:
        cal = json.load(f)

    # From calibration: how many pixels does 1mm of robot motion shift the bug?
    #   camera_axis_x = [dx_x, dy_x]: pixel shift when robot moved +delta_mm in X
    #   camera_axis_y = [dx_y, dy_y]: pixel shift when robot moved +delta_mm in Y
    # So: px_per_mm in robot-X direction = dx_x / delta  (signed — gives direction too)
    #     px_per_mm in robot-Y direction = dy_y / delta
    delta = cal["delta_mm"]
    dx_x  = cal["camera_axis_x"][0]   # camera-X shift per delta_mm robot-X move
    dy_y  = cal["camera_axis_y"][1]   # camera-Y shift per delta_mm robot-Y move

    px_per_mm_rx = dx_x / delta   # signed: how many camera-X px per robot-X mm
    px_per_mm_ry = dy_y / delta   # signed: how many camera-Y px per robot-Y mm

    print(f"\n  Calibration  px/mm: robot-X→cam-X = {px_per_mm_rx:.2f}   "
          f"robot-Y→cam-Y = {px_per_mm_ry:.2f}")

    # Connect robot
    robot = robot_connect()
    robot_home(robot)

    # Overhead snapshot
    print("\n  Overhead snapshot ...")
    ov_dev, ov_sys = camera_open(OVERHEAD_SERIAL, OVERHEAD_EXPOSURE_US, OVERHEAD_GAIN_DB)
    frame_ov = grab_frame(ov_dev)
    camera_close(ov_dev, ov_sys)

    from cib.vision.pipeline import run_overhead_detection
    result = run_overhead_detection(frame_ov)
    if not result.centroids_robot_xy:
        print("  [ERROR] No bugs found in overhead frame.")
        sys.exit(1)

    centroids = result.centroids_robot_xy
    print(f"\n  Found {len(centroids)} bug(s):")
    for i, (rx, ry) in enumerate(centroids):
        print(f"    [{i}]  X={rx:.3f}  Y={ry:.3f} mm")

    if len(centroids) == 1:
        idx = 0
    else:
        while True:
            raw = input(f"\n  Pick bug [0–{len(centroids)-1}] or ENTER for 0: ").strip()
            if raw == "":
                idx = 0; break
            try:
                idx = int(raw)
                if 0 <= idx < len(centroids):
                    break
            except ValueError:
                pass

    rx0, ry0 = centroids[idx]
    print(f"\n  Overhead centroid: X={rx0:.4f}  Y={ry0:.4f} mm")

    # Open EoA camera
    print("\n  Opening EoA camera ...")
    eoa_dev, eoa_sys = camera_open(EOA_SERIAL, EOA_EXPOSURE_US, EOA_GAIN_DB)

    try:
        # Step 1: move to overhead coord (rough)
        print(f"\n  Step 1 — moving to overhead centroid ...")
        robot_move(robot, rx0, ry0)
        robot_ring_on(robot)
        time.sleep(0.8)

        # Step 2: EoA detects bug, compute pixel offset from centre
        bug, frame = detect_bug(eoa_dev, "step-1")
        H, W = frame.shape[:2]
        cx, cy = W / 2.0, H / 2.0
        dpx = bug[0] - cx   # positive = bug is RIGHT of centre
        dpy = bug[1] - cy   # positive = bug is BELOW centre

        print(f"\n  Bug at pixel     : ({bug[0]:.1f}, {bug[1]:.1f})")
        print(f"  Image centre     : ({cx:.0f}, {cy:.0f})")
        print(f"  Pixel offset     : ({dpx:+.1f}, {dpy:+.1f}) px")

        # Step 3: convert pixel offset → robot mm correction
        # Robot must move so bug shifts from (bug_px) to (centre_px)
        # px_per_mm_rx tells us: 1mm robot-X → dx_x/delta camera-X pixels
        # So to shift bug by -dpx in camera-X: drx = -dpx / px_per_mm_rx
        drx = -dpx / px_per_mm_rx
        dry = -dpy / px_per_mm_ry

        print(f"  Robot correction : ({drx:+.4f}, {dry:+.4f}) mm")

        rx1 = rx0 + drx
        ry1 = ry0 + dry
        print(f"\n  Step 2 — moving to corrected position: "
              f"X={rx1:.4f}  Y={ry1:.4f} mm ...")
        robot_move(robot, rx1, ry1)
        time.sleep(0.8)

        # Step 4: verify
        bug_final, frame_final = detect_bug(eoa_dev, "final")
        H, W = frame_final.shape[:2]
        cx, cy = W / 2.0, H / 2.0
        residual_px  = np.sqrt((bug_final[0]-cx)**2 + (bug_final[1]-cy)**2)
        residual_mm  = residual_px / cal["px_per_mm"]

        print(f"""
{'=' * 65}
  RESULT

  Overhead coord     : ({rx0:.4f}, {ry0:.4f}) mm
  Corrected coord    : ({rx1:.4f}, {ry1:.4f}) mm
  Applied correction : ({drx:+.4f}, {dry:+.4f}) mm

  Bug pixel (final)  : ({bug_final[0]:.1f}, {bug_final[1]:.1f})
  Residual offset    : {residual_px:.1f} px  =  {residual_mm:.3f} mm
{'=' * 65}
""")

        show_frame(frame_final, bug_final,
                   title="EoA after correction — press any key",
                   info_lines=[
                       f"Corrected: ({rx1:.3f}, {ry1:.3f}) mm",
                       f"Residual: {residual_px:.0f} px = {residual_mm:.3f} mm",
                   ])

    finally:
        robot_ring_off(robot)
        robot_home(robot)
        camera_close(eoa_dev, eoa_sys)
        print("  Done.")


if __name__ == "__main__":
    main()
