"""
test_hold_position_for_u_jog.py — U-rotation drift test for the tool offset
=============================================================================

Same click-to-EoA flow as test_quadrant_click.py, but does NOT home the robot
on exit. The robot stays exactly where MOVE_IMAGE left it, so you can jog U
by hand on the teach pendant and watch whether the EoA crosshair stays on
the clicked point or drifts away.

WHY THIS TEST
  MOVE_IMAGE holds U fixed at whatever it currently is (sort_server.prg reads
  cur_u = CU(Here) and reuses it — it never rotates U on its own). So a single
  click only proves the tool offset is right for ONE U angle. This script lets
  you change U yourself afterward and see whether the TLSet 1 compensation
  (cx=17.1876, cy=-77.0233) correctly keeps the tool point fixed as U changes,
  or whether the camera swings away — which would point to a rotation-sign
  mismatch between how eoa_tool_offset.py solved for (cx, cy) and how SPEL+
  actually applies the Tool transform.

IMPORTANT — jog panel setting
  For this to test the RIGHT thing, jog U in RC+ using CARTESIAN/TOOL jog
  with Tool 1 selected, not JOINT jog. Joint-jogging U just rotates joint 4
  with the wrist held fixed — the camera WILL visibly swing in an arc, but
  that only shows the raw uncompensated geometry, not whether the software
  offset itself is correct. Tool jog (if available) is what should attempt
  to hold the tool (camera) point fixed while U changes — that's the actual
  behavior MOVE_IMAGE relies on.

BEFORE RUNNING
  1. Load sort_server.prg in RC+ and press F5 (must be PC mode, not Remote).
  2. Run  camera_calibration/eoa_live_feed.py  in a second terminal.
  3. Run this script, click ONE point (avoid a corner — pick something central
     so quadrant reachability isn't a factor yet).

Controls:
  Left-click  → EoA moves to that spot (does NOT home afterward)
  R           → retake overhead snapshot
  Q / Esc     → quit WITHOUT homing — robot stays exactly where it is

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/test_hold_position_for_u_jog.py
"""

import ctypes
import os
import socket
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP         = "172.16.9.15"
ROBOT_PORT       = 2000
ROBOT_TIMEOUT    = 30.0

OVERHEAD_SERIAL      = "253102168"
OVERHEAD_EXPOSURE_US = 60000
OVERHEAD_GAIN_DB     = 0.0

H_PATH        = os.path.join(os.path.dirname(__file__), "..", "camera_calibration", "output", "calibration_overhead.npy")
DISPLAY_WIDTH = 1400


# ── Robot ─────────────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket:
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
        print(f"  [ERROR] Unexpected response: {resp!r}")
        sys.exit(1)
    print("  Robot ready.")
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


def send_cmd(s: socket.socket, cmd: str) -> str:
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def robot_move_image(s: socket.socket, rx: float, ry: float) -> tuple[float, float]:
    resp = send_cmd(s, f"MOVE_IMAGE {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


# ── Camera ────────────────────────────────────────────────────────────────────

def grab_overhead(exposure_us: float = OVERHEAD_EXPOSURE_US,
                  gain_db: float = OVERHEAD_GAIN_DB) -> np.ndarray:
    try:
        from arena_api.system import system
    except ImportError:
        print("  [ERROR] arena_api not found. Use cib_env python.")
        sys.exit(1)

    print("  Waiting for overhead camera ...")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(str(i.get("serial", "")) == OVERHEAD_SERIAL
               for i in list(system.device_infos)):
            break
        time.sleep(0.5)

    info = next((i for i in list(system.device_infos)
                 if str(i.get("serial", "")) == OVERHEAD_SERIAL), None)
    if info is None:
        print(f"  [ERROR] Camera {OVERHEAD_SERIAL} not found.")
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

    for _ in range(4):
        b = dev.get_buffer(timeout=5000)
        dev.requeue_buffer(b)
    b   = dev.get_buffer(timeout=5000)
    bpp = b.bits_per_pixel // 8
    h, w = b.height, b.width
    raw = (ctypes.c_uint8 * (h * w * bpp)).from_address(ctypes.addressof(b.pbytes))
    frame = np.ctypeslib.as_array(raw).copy().reshape(h, w, bpp)
    dev.requeue_buffer(b)
    dev.stop_stream()
    system.destroy_device()
    print(f"  Overhead captured: {w}x{h} px")
    return frame


# ── Homography ────────────────────────────────────────────────────────────────

def pixel_to_robot(px: float, py: float, H: np.ndarray) -> tuple[float, float]:
    pt  = np.array([[[px, py]]], dtype=np.float32)
    out = cv2.perspectiveTransform(pt, H)
    return float(out[0][0][0]), float(out[0][0][1])


# ── Display ───────────────────────────────────────────────────────────────────

def build_display(frame: np.ndarray, clicks: list, scale: float) -> np.ndarray:
    dh = int(frame.shape[0] * scale)
    dw = int(frame.shape[1] * scale)
    disp = cv2.resize(frame, (dw, dh)).copy()

    for dx, dy, rx, ry in clicks:
        cv2.circle(disp, (dx, dy), 10, (0, 255, 0), -1)
        cv2.putText(disp, f"({rx:.0f},{ry:.0f})",
                    (dx + 12, dy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    cv2.rectangle(disp, (0, 0), (dw, 36), (20, 20, 20), -1)
    cv2.putText(disp, "Click = move EoA there (stays put)  |  R = refresh  |  Q = quit (NO home)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 210, 255), 2)

    return disp


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 70)
    print("  CIB — Hold Position for U-Jog Drift Test")
    print("=" * 70)
    print("""
  Click ONE point (pick something central, not a corner). The robot moves
  there and STAYS — this script will NOT home it on exit.

  Then, on the teach pendant: jog U using CARTESIAN/TOOL jog with Tool 1
  selected (not JOINT jog — that bypasses the offset compensation entirely
  and will just show the raw arm-swing, which tells you nothing new).

  Watch eoa_live_feed.py while you jog. If the crosshair stays on the same
  spot as U changes, the tool offset math is correct. If it drifts, that
  points to a rotation-sign mismatch in how (cx, cy) gets applied.

  CHECKLIST:
    - sort_server.prg running in RC+ (F5, PC mode not Remote)
    - eoa_live_feed.py open in a second terminal
    - Robot in AUTO mode for this script; switch to TEACH only when you jog
""")

    if not os.path.exists(H_PATH):
        print(f"  [ERROR] {H_PATH} not found. Run step2_full_calibration.py first.")
        sys.exit(1)
    H = np.load(H_PATH)
    print(f"  Loaded H matrix: {H_PATH}")

    sock = robot_connect()

    print()
    frame = grab_overhead()

    scale  = DISPLAY_WIDTH / frame.shape[1]
    clicks: list[tuple[int, int, float, float]] = []
    busy   = [False]

    WIN = "Hold Position — Click to move EoA (stays put)"

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if busy[0]:
            print("  [WAIT] still moving ...")
            return

        full_x = x / scale
        full_y = y / scale
        rx, ry = pixel_to_robot(full_x, full_y, H)

        print(f"\n  Click  display({x},{y})  target robot({rx:.2f}, {ry:.2f}) mm")
        print("  Sending MOVE_IMAGE ...")

        busy[0] = True
        try:
            ax, ay = robot_move_image(sock, rx, ry)
            print(f"  Target : ({rx:.2f}, {ry:.2f}) mm")
            print(f"  Robot reports tool at: ({ax:.2f}, {ay:.2f}) mm")
            print(f"  Delta  : ({ax - rx:+.2f}, {ay - ry:+.2f}) mm")
            print("  Check U on the teach pendant now, before jogging.")
            print("  Robot will STAY here — no auto-home.")
            clicks.append((x, y, rx, ry))
            disp = build_display(frame, clicks, scale)
            cv2.imshow(WIN, disp)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        busy[0] = False

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(frame.shape[0] * scale))
    cv2.setMouseCallback(WIN, on_click)

    print("  Overhead image open. Click one point, then Q to quit (no home).\n")

    nonlocal_frame = [frame]

    while True:
        disp = build_display(nonlocal_frame[0], clicks, scale)
        cv2.imshow(WIN, disp)
        key = cv2.waitKey(100) & 0xFF

        if key in (ord("q"), ord("Q"), 27):
            break

        if key in (ord("r"), ord("R")):
            if busy[0]:
                print("  [WAIT] robot busy, can't refresh now")
                continue
            print("\n  Refreshing overhead snapshot ...")
            try:
                nonlocal_frame[0] = grab_overhead()
                clicks.clear()
                print("  Snapshot refreshed.")
            except Exception as exc:
                print(f"  [ERROR] {exc}")

    cv2.destroyAllWindows()

    # Deliberately NOT calling robot_home() — robot stays exactly where it is
    # so you can jog U by hand and watch the EoA feed.
    print("\n  Closing connection — robot NOT homed, staying in position.")
    try:
        send_cmd(sock, "BYE")
    except Exception:
        pass
    sock.close()
    print("  Done. Go jog U on the teach pendant and watch eoa_live_feed.py.")


if __name__ == "__main__":
    main()
