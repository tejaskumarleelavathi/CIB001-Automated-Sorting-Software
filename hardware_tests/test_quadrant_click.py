"""
test_quadrant_click.py — Click-to-EoA quadrant test (tool offset validation)
=============================================================================

Shows the overhead camera image. Click anywhere → robot's EoA camera
moves there using MOVE_IMAGE, which respects the TLSet tool offset set
at startup in sort_server.prg.

Use this to validate all 4 dish quadrants. After each click, watch
eoa_live_feed.py to confirm the EoA crosshair lands on the clicked spot.
Note the U angle shown on the teach pendant for each quadrant.

BEFORE RUNNING:
  1. Load sort_server.prg in RC+ and press F5.
  2. Run:  python camera_calibration/eoa_live_feed.py  in a second terminal.
  3. Run this script.

Controls:
  Left-click  → EoA moves to that spot
  R           → retake overhead snapshot
  Q / Esc     → home robot and quit

Usage:
  python hardware_tests/test_quadrant_click.py
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

OVERHEAD_SERIAL  = "253102168"
OVERHEAD_EXPOSURE_US = 60000
OVERHEAD_GAIN_DB     = 0.0

H_PATH       = os.path.join(os.path.dirname(__file__), "..", "camera_calibration", "output", "calibration_overhead_tooloffset.npy")
DISPLAY_WIDTH = 1400

# Quadrant label positions (fraction of image width/height)
QUADRANT_LABELS = [
    (0.15, 0.15, "TL"),
    (0.85, 0.15, "TR"),
    (0.15, 0.85, "BL"),
    (0.85, 0.85, "BR"),
]


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
    print(f"  Robot ready.")
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


def robot_home(s: socket.socket) -> None:
    print("  Homing robot ...")
    try:
        send_cmd(s, "HOME")
        print("  Homed.")
    except Exception as exc:
        print(f"  [WARN] HOME failed: {exc}")


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

    # Drain stale frames, grab fresh one
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


# ── Display helpers ───────────────────────────────────────────────────────────

def build_display(frame: np.ndarray, clicks: list, scale: float) -> np.ndarray:
    dh = int(frame.shape[0] * scale)
    dw = int(frame.shape[1] * scale)
    disp = cv2.resize(frame, (dw, dh)).copy()

    # Quadrant labels
    for fx, fy, label in QUADRANT_LABELS:
        lx, ly = int(dw * fx), int(dh * fy)
        cv2.putText(disp, label, (lx - 20, ly),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 200, 255), 3)

    # Quadrant dividing lines (faint)
    cv2.line(disp, (dw // 2, 0), (dw // 2, dh), (80, 80, 80), 1)
    cv2.line(disp, (0, dh // 2), (dw, dh // 2), (80, 80, 80), 1)

    # Previous clicks
    for dx, dy, rx, ry in clicks:
        cv2.circle(disp, (dx, dy), 10, (0, 255, 0), -1)
        cv2.putText(disp, f"({rx:.0f},{ry:.0f})",
                    (dx + 12, dy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    # Instructions
    cv2.rectangle(disp, (0, 0), (dw, 36), (20, 20, 20), -1)
    cv2.putText(disp, "Click = move EoA there  |  R = refresh snapshot  |  Q = quit",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 210, 255), 2)

    return disp


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 60)
    print("  CIB — Click-to-EoA Quadrant Test (tool offset validation)")
    print("=" * 60)
    print("""
  sort_server.prg sets TLSet 1 (cx=17.19mm, cy=-77.02mm) at startup.
  Every MOVE_IMAGE command compensates for this offset automatically.

  CHECKLIST:
    ✓ sort_server.prg running in RC+ (F5)
    ✓ eoa_live_feed.py open in a second terminal
    ✓ Robot is NOT in teach mode (switch to AUTO)
""")

    # Load homography
    if not os.path.exists(H_PATH):
        print(f"  [ERROR] {H_PATH} not found. Run step2_full_calibration.py first.")
        sys.exit(1)
    H = np.load(H_PATH)
    print(f"  Loaded H matrix: {H_PATH}")

    # Connect robot
    sock = robot_connect()

    # Grab overhead snapshot (robot should already be at home/park from sort_server startup)
    print()
    frame = grab_overhead()

    scale  = DISPLAY_WIDTH / frame.shape[1]
    clicks: list[tuple[int, int, float, float]] = []  # (disp_x, disp_y, robot_x, robot_y)
    busy   = [False]

    WIN = "Quadrant Test — Click to move EoA"

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if busy[0]:
            print("  [WAIT] still moving ...")
            return

        # Display pixel → full-res pixel → robot XY
        full_x = x / scale
        full_y = y / scale
        rx, ry = pixel_to_robot(full_x, full_y, H)

        quadrant = ("TL" if full_x < frame.shape[1]/2 else "TR") \
                   if full_y < frame.shape[0]/2 else \
                   ("BL" if full_x < frame.shape[1]/2 else "BR")

        print(f"\n  Click → {quadrant}  display({x},{y})"
              f"  robot({rx:.2f}, {ry:.2f}) mm")
        print(f"  Sending MOVE_IMAGE ...")

        busy[0] = True
        try:
            ax, ay = robot_move_image(sock, rx, ry)
            print(f"  Robot at ({ax:.2f}, {ay:.2f}) mm  ← check U on teach pendant")
            print(f"  Watch eoa_live_feed.py — EoA crosshair should be on the clicked spot")
            clicks.append((x, y, rx, ry))
            disp = build_display(frame, clicks, scale)
            cv2.imshow(WIN, disp)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        busy[0] = False

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(frame.shape[0] * scale))
    cv2.setMouseCallback(WIN, on_click)

    print()
    print("  Overhead image open. Click the 4 dish corners one by one.")
    print("  After each click, note the U angle on the teach pendant.\n")

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
    robot_home(sock)
    send_cmd(sock, "BYE")
    sock.close()

    print()
    if clicks:
        print("  Clicks recorded this session:")
        print(f"  {'Quadrant':<10} {'Robot X':>10} {'Robot Y':>10}")
        print("  " + "-" * 34)
        for dx, dy, rx, ry in clicks:
            full_x = dx / scale
            full_y = dy / scale
            q = ("TL" if full_x < nonlocal_frame[0].shape[1]/2 else "TR") \
                if full_y < nonlocal_frame[0].shape[0]/2 else \
                ("BL" if full_x < nonlocal_frame[0].shape[1]/2 else "BR")
            print(f"  {q:<10} {rx:>10.2f} {ry:>10.2f}")
    print("\n  Done.")


if __name__ == "__main__":
    main()
