"""
eoa_pixel_scale.py — EoA camera pixel-scale calibration (fully automatic).
===========================================================================

CONCEPT
-------
The robot moves by a known distance (DELTA_MM) in X and Y.
The EoA camera sees where the bug shifts in pixels.
  px_per_mm = pixel_shift / DELTA_MM

FLOW
----
  1. Takes ONE overhead snapshot, runs vision pipeline, finds bugs.
  2. You pick which bug to use (or ENTER for auto).
  3. Overhead camera closes. Robot moves to that bug via MOVE_IMAGE_U at the
     given --u angle (held explicitly for every move in this script).
  4. EoA live feed opens — shows bug and crosshair. Press SPACE to confirm
     bug is visible and well-detected, then close the window.
  5. Script automatically:
       a. Captures bug pixel at position (rx, ry)          → P0
       b. Moves robot +DELTA_MM in X → captures             → Px
       c. Moves robot back, +DELTA_MM in Y → captures       → Py
       d. Moves robot back to (rx, ry)
  6. Computes:
       px_per_mm_x = |Px - P0| in pixels / DELTA_MM
       px_per_mm_y = |Py - P0| in pixels / DELTA_MM
  7. Shows live result, saves JSON.
  8. Robot homes.

No manual jogging. No teach pendant reading. No connection timeout.

WHY --u IS REQUIRED (2026-08-19, see DEV-13 in prd_updated.md)
----------------------------------------------------------------
The EoA camera physically rotates with the robot's U (wrist) angle, so the
pixel-to-mm mapping this script measures is only valid AT the U angle it was
measured at. Real PICK operations happen at one of two quadrant angles
(u_top_deg / u_bottom_deg in config/machine_config.yaml — currently 209.0
and 251.0), never at a fixed/arbitrary U. This script used to move with
plain MOVE_IMAGE (no U), which silently held whatever U the robot happened
to already be parked at — producing a calibration that didn't match either
real quadrant angle, and applying it uniformly to both. That's now fixed:
you MUST pass --u explicitly, and the output filename encodes it so the two
quadrant calibrations can coexist without overwriting each other.

OUTPUT
------
  camera_calibration/output/eoa_pixel_scale_u<U>.json  (e.g. _u209.json)

Usage:
  python hardware_tests/eoa_pixel_scale.py --u 209
  python hardware_tests/eoa_pixel_scale.py --u 251
"""

import argparse
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

# Backlight is left alone entirely by this script — stays ON throughout
# (explicit operator instruction, 2026-08-19). Do not add backlight control
# here without checking first.

# How far the robot moves to measure pixel scale.
# Larger = more accurate. 5mm is a good balance — enough pixel shift
# without moving the bug out of the EoA frame.
DELTA_MM = 2.0

OUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "camera_calibration", "output")
# OUT_JSON is set in main() once --u is parsed — filename encodes the U angle
# so per-quadrant calibrations don't overwrite each other.

# EoA blob detector — finds dark bug on lighter background (ring light only)
BLOB_THRESHOLD = 100    # pixels darker than this are "bug"
MIN_BLOB_AREA  = 300    # px² — ignore dust
MAX_BLOB_AREA  = 80000  # px² — ignore background patches

DISPLAY_WIDTH  = 1280   # live feed window width


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
        print("  Make sure sort_server.prg is running in RC+ (F5).")
        sys.exit(1)
    s.sendall(b"STATUS\r\n")
    resp = _recv_line(s)
    if "READY" not in resp:
        print(f"  [ERROR] Unexpected robot response: {resp!r}")
        sys.exit(1)
    print(f"  Robot ready: {resp}")
    return s


def robot_move_image(s, rx, ry, u):
    """
    Always holds an explicit U (MOVE_IMAGE_U) — never the bare MOVE_IMAGE,
    which silently holds whatever U the robot happens to already be at.
    This calibration is only valid at the U it was measured at (the EoA
    camera physically rotates with U), so every move in a run must use the
    SAME, explicitly-specified angle. See DEV-13.
    """
    cmd = f"MOVE_IMAGE_U {rx:.3f},{ry:.3f},{u:.3f}"
    print(f"  Robot → {cmd} ...")
    resp = _send(s, cmd)
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE_U failed: {resp!r}")


def robot_ring_on(s):
    resp = _send(s, "RING_ON")
    if "ERR" in resp:
        print(f"  [WARN] Ring light: {resp!r}")


def robot_ring_off(s):
    try:
        _send(s, "RING_OFF")
    except Exception:
        pass


def robot_home(s):
    print("  Robot → HOME ...")
    try:
        _send(s, "HOME")
        print("  Homed.")
    except Exception as exc:
        print(f"  [WARN] HOME failed ({exc}) — home manually in RC+.")


# ── Camera helpers ────────────────────────────────────────────────────────────

def camera_open(serial, exposure_us, gain_db, n_buffers=4):
    from arena_api.system import system

    print(f"  Waiting for camera {serial} ...")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(str(i.get("serial", "")) == serial for i in list(system.device_infos)):
            break
        time.sleep(0.5)

    info = next(
        (i for i in list(system.device_infos) if str(i.get("serial", "")) == serial),
        None
    )
    if info is None:
        print(f"  [ERROR] Camera {serial} not found.")
        sys.exit(1)

    dev = system.create_device(device_infos=info)[0]
    nm  = dev.nodemap
    for k, v in [
        ("PixelFormat",  "BGR8"),
        ("ExposureAuto", "Off"),
        ("ExposureTime", float(exposure_us)),
        ("GainAuto",     "Off"),
        ("Gain",         float(gain_db)),
    ]:
        try:
            nm[k].value = v
        except Exception:
            pass
    dev.start_stream(n_buffers)
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
    """Drain 2 stale buffers, return 1 fresh frame."""
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


# ── EoA blob detection ────────────────────────────────────────────────────────

def find_bug(frame):
    """
    Find the dark blob closest to the image centre in an EoA frame (ring light only).
    The robot moves its tool centre to the target bug, so the correct bug should be
    near the crosshair — not necessarily the largest blob in the frame.
    Returns (cx, cy) float pixels, or None.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame.copy()
    _, mask = cv2.threshold(gray, BLOB_THRESHOLD, 255, cv2.THRESH_BINARY_INV)
    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k, iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid = [c for c in contours if MIN_BLOB_AREA < cv2.contourArea(c) < MAX_BLOB_AREA]
    if not valid:
        return None
    img_cx, img_cy = frame.shape[1] / 2.0, frame.shape[0] / 2.0
    def centroid(c):
        M = cv2.moments(c)
        if M["m00"] == 0:
            return None
        return (M["m10"] / M["m00"], M["m01"] / M["m00"])
    centroids = [(c, centroid(c)) for c in valid]
    centroids = [(c, ct) for c, ct in centroids if ct is not None]
    if not centroids:
        return None
    best_c, best_ct = min(centroids, key=lambda x: (x[1][0]-img_cx)**2 + (x[1][1]-img_cy)**2)
    return best_ct


# ── Overhead bug-number preview ─────────────────────────────────────────────

def show_labeled_overhead(frame_ov, annotated_frame, centroids_px):
    """
    Show the overhead snapshot with each detected bug circled and numbered
    (matching the [0], [1], ... indices printed to the terminal), so the
    numeric selection prompt isn't a blind guess. Press any key to close.

    Takes explicit centroids_px (not a whole result object) so the caller
    can pass an already quadrant-filtered list — only the bugs actually
    selectable get drawn/numbered.
    """
    vis = annotated_frame if annotated_frame is not None else frame_ov
    vis = vis.copy()
    if vis.ndim == 2:
        vis = cv2.cvtColor(vis, cv2.COLOR_GRAY2BGR)

    h, w = vis.shape[:2]
    r = max(20, w // 150)
    fs = max(1.5, w / 2600)
    th = max(3, int(fs * 3))
    for i, (px, py) in enumerate(centroids_px):
        cx, cy = int(px), int(py)
        cv2.circle(vis, (cx, cy), r, (0, 0, 255), th)
        cv2.putText(vis, str(i), (cx + r + 10, cy + r // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 255), th)

    win = "Overhead — bug numbers (press any key to continue)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    disp_w = DISPLAY_WIDTH
    cv2.resizeWindow(win, disp_w, int(disp_w * h / w))
    disp = cv2.resize(vis, (disp_w, int(disp_w * h / w)), interpolation=cv2.INTER_AREA)
    cv2.imshow(win, disp)
    print("\n  Bug-number preview shown — press any key in that window to continue.")
    cv2.waitKey(0)
    cv2.destroyWindow(win)


# ── Live preview ──────────────────────────────────────────────────────────────

def show_preview(dev, title, instruction):
    """
    Show EoA live feed with crosshair + bug marker.
    Press SPACE to confirm bug is detected and visible.
    Press Q to quit.
    Returns True if confirmed, False if quit.
    """
    WIN = title
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(DISPLAY_WIDTH * 4600 / 5320))

    print(f"\n  {instruction}")
    print("  SPACE = confirm  |  Q = quit\n")

    while True:
        frame = grab_frame(dev)
        h, w  = frame.shape[:2]
        cx_img, cy_img = w // 2, h // 2
        vis   = frame.copy()

        bug = find_bug(frame)

        # Bug marker — RED
        if bug:
            bx, by = int(bug[0]), int(bug[1])
            cv2.circle(vis, (bx, by), 20, (0, 0, 255), 3)
            cv2.line(vis, (bx-30, by), (bx+30, by), (0, 0, 255), 2)
            cv2.line(vis, (bx, by-30), (bx, by+30), (0, 0, 255), 2)
            offset_txt = f"Bug offset from centre: ({bx - cx_img:+d}, {by - cy_img:+d}) px"
            cv2.putText(vis, offset_txt, (20, h-120),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)
        else:
            cv2.putText(vis, "No bug detected — check lighting / BLOB_THRESHOLD",
                        (20, h-120), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 100, 255), 3)

        # Crosshair — GREEN (image centre = robot tool centre)
        cs = 300
        cv2.line(vis, (cx_img-cs, cy_img), (cx_img+cs, cy_img), (0, 255, 0), 5)
        cv2.line(vis, (cx_img, cy_img-cs), (cx_img, cy_img+cs), (0, 255, 0), 5)
        cv2.circle(vis, (cx_img, cy_img), 40, (0, 255, 0), 4)
        cv2.circle(vis, (cx_img, cy_img), 8,  (0, 255, 0), -1)

        # Instruction overlay
        cv2.putText(vis, instruction, (20, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 0), 3)
        cv2.putText(vis, "SPACE=confirm  Q=quit", (20, h-40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)

        sf   = DISPLAY_WIDTH / w
        disp = cv2.resize(vis, (DISPLAY_WIDTH, int(h * sf)), interpolation=cv2.INTER_AREA)
        cv2.imshow(WIN, disp)
        key = cv2.waitKey(30) & 0xFF

        if key in (ord('q'), ord('Q'), 27):
            cv2.destroyWindow(WIN)
            return False

        if key == ord(' '):
            if bug is None:
                print("  [WARN] No bug detected — press Q to quit or wait for detection.")
                continue
            print(f"  Confirmed. Bug at ({bug[0]:.1f}, {bug[1]:.1f}) px")
            cv2.destroyWindow(WIN)
            return True

    cv2.destroyWindow(WIN)
    return False


# ── Capture + detect at current robot position ────────────────────────────────

def capture_bug_px(dev, label):
    """
    Capture a fresh frame, detect bug, return (cx, cy) or raise.
    Retries up to 3 times with progressively relaxed threshold before giving up.
    """
    global BLOB_THRESHOLD
    original_threshold = BLOB_THRESHOLD
    for attempt in range(3):
        frame = grab_frame(dev)
        bug   = find_bug(frame)
        if bug is not None:
            if attempt > 0:
                print(f"  [{label}] detected on retry {attempt} "
                      f"(threshold={BLOB_THRESHOLD})")
            BLOB_THRESHOLD = original_threshold
            print(f"  [{label}] bug at pixel ({bug[0]:.1f}, {bug[1]:.1f})")
            return bug
        # Relax threshold by 20 each retry (captures lighter/faded bugs)
        BLOB_THRESHOLD = min(BLOB_THRESHOLD + 20, 200)
        print(f"  [{label}] no detection — retrying with threshold={BLOB_THRESHOLD} ...")
        time.sleep(0.3)
    BLOB_THRESHOLD = original_threshold
    raise RuntimeError(f"Bug not detected at position {label} after 3 attempts. "
                       "Check lighting or repositioning.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--u", type=float, required=True,
                     help="Wrist U angle (deg) to hold for this calibration — "
                          "must match a real quadrant angle (u_top_deg/u_bottom_deg "
                          "in config/machine_config.yaml), not an arbitrary value.")
    args = ap.parse_args()
    u = args.u

    out_json = os.path.join(OUT_DIR, f"eoa_pixel_scale_u{u:g}.json")

    print("=" * 65)
    print("  EoA Camera Pixel Scale Calibration (fully automatic)")
    print(f"  Robot will move ±{DELTA_MM}mm to measure pixel scale.")
    print(f"  Holding U = {u}° for every move — output: {out_json}")
    print("=" * 65)

    # ── Connect robot and home FIRST ─────────────────────────────────────────
    # Home before taking overhead snapshot so the arm is out of the camera view.
    robot = robot_connect()
    robot_home(robot)

    # ── Overhead snapshot ─────────────────────────────────────────────────────
    print("\n  Opening overhead camera ...")
    ov_dev, ov_sys = camera_open(OVERHEAD_SERIAL, OVERHEAD_EXPOSURE_US, OVERHEAD_GAIN_DB)

    print("  Capturing overhead snapshot ...")
    frame_ov = grab_frame(ov_dev)
    camera_close(ov_dev, ov_sys)
    print("  Overhead camera closed.\n")

    # Run vision
    print("  Running overhead vision ...")
    from cib.vision.pipeline import run_overhead_detection
    result = run_overhead_detection(frame_ov)

    if not result.centroids_robot_xy:
        print("  [ERROR] No bugs detected. Make sure backlight is on and bugs are in the dish.")
        sys.exit(1)

    # ── Quadrant filter — only show/allow bugs that actually belong to the
    # U angle being calibrated, so you can't accidentally calibrate U=209
    # using a bug that's really in the bottom quadrant (see DEV-13/14:
    # u_top_deg/u_bottom_deg, config/machine_config.yaml).
    from cib.config.loader import load_machine_config
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "machine_config.yaml")
    robot_cfg = load_machine_config(cfg_path).robot
    split = robot_cfg.quadrant_split_y_mm

    all_xy = result.centroids_robot_xy
    all_px = result.centroids_px
    if split is not None and robot_cfg.u_top_deg is not None and robot_cfg.u_bottom_deg is not None:
        if abs(u - robot_cfg.u_top_deg) < 0.01:
            keep = [i for i, (_, ry) in enumerate(all_xy) if ry >= split]
            quadrant_name = f"TOP (y >= {split})"
        elif abs(u - robot_cfg.u_bottom_deg) < 0.01:
            keep = [i for i, (_, ry) in enumerate(all_xy) if ry < split]
            quadrant_name = f"BOTTOM (y < {split})"
        else:
            print(f"  [WARN] --u {u} matches neither u_top_deg ({robot_cfg.u_top_deg}) "
                  f"nor u_bottom_deg ({robot_cfg.u_bottom_deg}) — showing all bugs, unfiltered.")
            keep = list(range(len(all_xy)))
            quadrant_name = "ALL (unfiltered — U doesn't match a configured quadrant)"
    else:
        print("  [WARN] quadrant_split_y_mm/u_top_deg/u_bottom_deg not set in machine_config.yaml "
              "— showing all bugs, unfiltered.")
        keep = list(range(len(all_xy)))
        quadrant_name = "ALL (unfiltered — quadrant config missing)"

    centroids = [all_xy[i] for i in keep]
    centroids_px = [all_px[i] for i in keep]

    if not centroids:
        print(f"  [ERROR] No bugs found in the {quadrant_name} quadrant for U={u}, "
              f"out of {len(all_xy)} detected total. Showing ALL detected bugs "
              f"(unfiltered, numbered) so you can check whether this is a real "
              f"detection gap or a top/bottom labeling mismatch ...")
        for i, (rx, ry) in enumerate(all_xy):
            print(f"    [{i}]  X={rx:.3f}  Y={ry:.3f} mm"
                  f"  ({'>=' if ry >= split else '<'} {split} -> "
                  f"{'TOP' if ry >= split else 'BOTTOM'})")
        show_labeled_overhead(frame_ov, result.annotated_frame, all_px)
        sys.exit(1)

    print(f"\n  Found {len(all_xy)} bug(s) total — {len(centroids)} in quadrant {quadrant_name}:")
    for i, (rx, ry) in enumerate(centroids):
        print(f"    [{i}]  X={rx:.3f}  Y={ry:.3f} mm")

    show_labeled_overhead(frame_ov, result.annotated_frame, centroids_px)

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

    target_rx, target_ry = centroids[idx]
    print(f"\n  Target: X={target_rx:.3f}  Y={target_ry:.3f} mm")

    # Backlight stays ON throughout — explicit operator instruction 2026-08-19.
    # Do NOT turn it off here. (Ring light is still turned on separately below.)

    # ── Open EoA camera ───────────────────────────────────────────────────────
    print("\n  Opening EoA camera ...")
    eoa_dev, eoa_sys = camera_open(EOA_SERIAL, EOA_EXPOSURE_US, EOA_GAIN_DB)

    try:
        # Move to bug, ring light on
        print(f"\n  Moving robot to bug ...")
        robot_move_image(robot, target_rx, target_ry, u)
        robot_ring_on(robot)
        time.sleep(0.5)  # let ring light stabilise

        # ── Preview: confirm bug visible ──────────────────────────────────────
        confirmed = show_preview(
            eoa_dev,
            title="EoA Preview — confirm bug visible, then press SPACE",
            instruction="Confirm bug visible (RED dot). SPACE to start auto-calibration."
        )
        if not confirmed:
            print("\n  Quit — no calibration saved.")
            return

        # ── Auto-calibration: 3 robot positions ──────────────────────────────
        print(f"\n  Auto-calibration: moving robot ±{DELTA_MM}mm ...")

        # Position 0: current (rx, ry) — baseline
        time.sleep(0.3)
        p0 = capture_bug_px(eoa_dev, "P0 baseline")

        # Position X+: move +DELTA in X
        robot_move_image(robot, target_rx + DELTA_MM, target_ry, u)
        time.sleep(0.5)
        px_plus = capture_bug_px(eoa_dev, f"Px +{DELTA_MM}mm X")

        # Back to baseline, then Y: try +Y first, fall back to -Y if not detected
        robot_move_image(robot, target_rx, target_ry, u)
        time.sleep(0.3)
        y_sign = +1
        robot_move_image(robot, target_rx, target_ry + y_sign * DELTA_MM, u)
        time.sleep(0.8)
        try:
            py_plus = capture_bug_px(eoa_dev, f"Py +{DELTA_MM}mm Y")
        except RuntimeError:
            print(f"  [WARN] +Y detection failed — trying -Y ...")
            robot_move_image(robot, target_rx, target_ry, u)
            time.sleep(0.3)
            y_sign = -1
            robot_move_image(robot, target_rx, target_ry + y_sign * DELTA_MM, u)
            time.sleep(0.8)
            py_plus = capture_bug_px(eoa_dev, f"Py -{DELTA_MM}mm Y")

        # Return to baseline
        robot_move_image(robot, target_rx, target_ry, u)

        # ── Calculate ─────────────────────────────────────────────────────────
        # Pixel shifts when robot moves +DELTA_MM
        shift_x_px = np.sqrt((px_plus[0]-p0[0])**2 + (px_plus[1]-p0[1])**2)
        shift_y_px = np.sqrt((py_plus[0]-p0[0])**2 + (py_plus[1]-p0[1])**2)

        px_per_mm_x = shift_x_px / DELTA_MM
        px_per_mm_y = shift_y_px / DELTA_MM
        px_per_mm   = (px_per_mm_x + px_per_mm_y) / 2.0
        mm_per_px   = 1.0 / px_per_mm

        # Direction of each shift (sign tells us camera axis orientation)
        dx_x = px_plus[0] - p0[0]
        dy_x = px_plus[1] - p0[1]
        dx_y = py_plus[0] - p0[0]
        dy_y = py_plus[1] - p0[1]

        print(f"""
{'=' * 65}
  RESULT

  P0  (baseline)  : ({p0[0]:.1f},      {p0[1]:.1f}) px
  Px  (+{DELTA_MM}mm X) : ({px_plus[0]:.1f},  {px_plus[1]:.1f}) px  shift=({dx_x:+.1f}, {dy_x:+.1f}) px
  Py  (+{DELTA_MM}mm Y) : ({py_plus[0]:.1f},  {py_plus[1]:.1f}) px  shift=({dx_y:+.1f}, {dy_y:+.1f}) px

  px/mm  X-axis   : {px_per_mm_x:.2f}  (|shift| = {shift_x_px:.1f} px over {DELTA_MM}mm)
  px/mm  Y-axis   : {px_per_mm_y:.2f}  (|shift| = {shift_y_px:.1f} px over {DELTA_MM}mm)
  px/mm  average  : {px_per_mm:.2f} px/mm
  µm/px  average  : {mm_per_px*1000:.2f} µm/px

  X/Y scale match : {abs(px_per_mm_x - px_per_mm_y) / px_per_mm * 100:.1f}% difference
  (should be <5% — if higher, re-run or adjust DELTA_MM)
{'=' * 65}
""")

        # ── Save ──────────────────────────────────────────────────────────────
        os.makedirs(OUT_DIR, exist_ok=True)
        out = {
            "u_deg":            u,
            "px_per_mm":        round(px_per_mm, 4),
            "mm_per_px":        round(mm_per_px, 6),
            "px_per_mm_x":      round(px_per_mm_x, 4),
            "px_per_mm_y":      round(px_per_mm_y, 4),
            "delta_mm":         DELTA_MM,
            "p0_px":            [round(p0[0], 1),       round(p0[1], 1)],
            "px_plus_px":       [round(px_plus[0], 1),  round(px_plus[1], 1)],
            "py_plus_px":       [round(py_plus[0], 1),  round(py_plus[1], 1)],
            "shift_x_px":       round(shift_x_px, 2),
            "shift_y_px":       round(shift_y_px, 2),
            "robot_target_mm":  [round(target_rx, 4),   round(target_ry, 4)],
            "camera_axis_x":    [round(dx_x, 2),        round(dy_x, 2)],
            "camera_axis_y":    [round(dx_y, 2),        round(dy_y, 2)],
        }
        with open(out_json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"  Saved: {out_json}")

    finally:
        robot_ring_off(robot)
        robot_home(robot)
        camera_close(eoa_dev, eoa_sys)
        print("  Done.")


if __name__ == "__main__":
    main()
