"""
test_quadrant_u_select.py — quadrant-based U selection test
=============================================================================

Click anywhere on the overhead image. The script picks a U angle based on
which half of the dish the click is in (top 2 quadrants vs bottom 2), then
sends MOVE_U (explicit x, y, u) so the robot moves to that U before/while
reaching the target — instead of MOVE_IMAGE, which just holds whatever U
the robot currently happens to be at.

This is the next step after confirming the tool offset + calibration are
correct (see calibration_overhead_tooloffset.npy, RMSE=0.025mm, verified
U-independent 2026-08-04). Now that Tool 1 reliably lands the camera on
target at ANY U, this script tests picking the RIGHT U per quadrant so the
arm can physically reach every quadrant without joint-limit issues.

FILL IN BEFORE RUNNING
  TOP_U and BOTTOM_U below are placeholders — set them to the U angles you
  want for the top-2 vs bottom-2 quadrants.

USES calibration_v3_tooloffset_server.prg (MOVE_U command, Tool 1 active) —
NOT sort_server.prg (which only has MOVE_IMAGE, no explicit-U command).

BEFORE RUNNING
  1. Load calibration_v3_tooloffset_server.prg in RC+, press F5.
  2. Run eoa_live_feed.py in a second terminal.
  3. Fill in TOP_U / BOTTOM_U below.

Controls:
  Left-click  -> EoA moves to that spot at the quadrant's assigned U
  L           -> switch to LEFTY quadrant U's (177.718 top / 86.053 bottom)
  Y           -> switch back to RIGHTY quadrant U's (209.0 top / 251.0 bottom)
  D           -> drop test: pipette (Tool 2) to Station 3's H12 well,
                 Lefty, U=200.023 (left_home's U -- NOT the quadrant-pick
                 U, e.g. 86.053, which is not safe to drop at)
  R           -> retake overhead snapshot
  Q / Esc     -> quit (does NOT home)

2026-08-26 update: quadrant U now depends on which hand is active (L/Y
keys above) -- picking the same physical quadrant needs a DIFFERENT U in
Lefty vs Righty. MOVE_U now sends an explicit hand too (real-hardware
finding elsewhere in this project: a bare Go can silently pick the wrong
hand even when it looks like it shouldn't -- see
test_plate_station_positions.prg's history for the full story).

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/test_quadrant_u_select.py
"""

import ctypes
import os
import socket
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── FILL THESE IN ────────────────────────────────────────────────────────────
# Righty (existing production values) and Lefty (2026-08-26, added for the
# left-side plate stations) -- toggle at runtime with the L / Y keys.
RIGHTY_TOP_U    = 209.0    # U angle (deg) for TL + TR quadrants, Righty
RIGHTY_BOTTOM_U = 251.0    # U angle (deg) for BL + BR quadrants, Righty
LEFTY_TOP_U     = 177.718  # U angle (deg) for TL + TR quadrants, Lefty
LEFTY_BOTTOM_U  = 86.053   # U angle (deg) for BL + BR quadrants, Lefty

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP         = "172.16.9.15"
ROBOT_PORT       = 2000
ROBOT_TIMEOUT    = 30.0

OVERHEAD_SERIAL      = "253102168"
OVERHEAD_EXPOSURE_US = 60000
OVERHEAD_GAIN_DB     = 0.0

# Use the CORRECTED tool-offset-based calibration, not the old wrist-based one.
H_PATH        = os.path.join(os.path.dirname(__file__), "..", "camera_calibration",
                             "output", "calibration_overhead_tooloffset.npy")
DISPLAY_WIDTH = 1400

# NOTE: image-top is physically the BOTTOM of the dish (camera mounting/
# orientation), so labels are flipped vs raw image position — this matches
# what you actually see standing at the robot, not raw pixel row.
QUADRANT_LABELS = [
    (0.15, 0.15, "BL"),
    (0.85, 0.15, "BR"),
    (0.15, 0.85, "TL"),
    (0.85, 0.85, "TR"),
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
        print("  Make sure calibration_v3_tooloffset_server.prg is running in RC+ (F5).")
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


def robot_move_u(s: socket.socket, rx: float, ry: float, u: float, hand: str) -> tuple[float, float, float]:
    resp = send_cmd(s, f"MOVE_U {rx:.3f},{ry:.3f},{u:.3f},{hand}")
    if not resp.startswith("OK MOVE_U"):
        raise RuntimeError(f"MOVE_U failed: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3]), float(parts[4])


def robot_drop_test(s: socket.socket) -> None:
    """D key -- pipette to Station 3's H12, Lefty, U=200.023 (left_home's
    U). Position only, no actuation. See DROP_TEST in
    calibration_v3_tooloffset_server.prg for the full explanation."""
    resp = send_cmd(s, "DROP_TEST")
    if not resp.startswith("OK DROP_TEST"):
        raise RuntimeError(f"DROP_TEST failed: {resp!r}")


def robot_home(s: socket.socket) -> None:
    print("  Homing robot ...")
    try:
        resp = send_cmd(s, "HOME")
        if not resp.startswith("OK HOME"):
            print(f"  [WARN] Unexpected HOME response: {resp!r}")
        else:
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


def quadrant_and_u(full_x: float, full_y: float, img_w: int, img_h: int,
                    hand: str) -> tuple[str, float]:
    """
    Image-top half -> BOTTOM_U, image-bottom half -> TOP_U.
    Image-top is physically the BOTTOM of the dish (camera mounting), so this
    is deliberately inverted vs raw image row — matches physical reality.

    hand: "R" or "L" -- picks the Righty or Lefty U pair (2026-08-26).
    """
    is_top_physical = full_y >= img_h / 2   # image-bottom = physical top
    is_left = full_x < img_w / 2
    quadrant = ("TL" if is_left else "TR") if is_top_physical else ("BL" if is_left else "BR")
    top_u, bottom_u = (LEFTY_TOP_U, LEFTY_BOTTOM_U) if hand == "L" else (RIGHTY_TOP_U, RIGHTY_BOTTOM_U)
    u = top_u if is_top_physical else bottom_u
    return quadrant, u


# ── Display ───────────────────────────────────────────────────────────────────

def build_display(frame: np.ndarray, clicks: list, scale: float, hand: str) -> np.ndarray:
    dh = int(frame.shape[0] * scale)
    dw = int(frame.shape[1] * scale)
    disp = cv2.resize(frame, (dw, dh)).copy()

    for fx, fy, label in QUADRANT_LABELS:
        lx, ly = int(dw * fx), int(dh * fy)
        cv2.putText(disp, label, (lx - 20, ly),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 200, 255), 3)

    cv2.line(disp, (dw // 2, 0), (dw // 2, dh), (80, 80, 80), 1)
    cv2.line(disp, (0, dh // 2), (dw, dh // 2), (80, 80, 80), 1)

    for dx, dy, rx, ry, u, q in clicks:
        cv2.circle(disp, (dx, dy), 10, (0, 255, 0), -1)
        cv2.putText(disp, f"{q} U={u:.0f}",
                    (dx + 12, dy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    cv2.rectangle(disp, (0, 0), (dw, 60), (20, 20, 20), -1)
    hand_label = "LEFTY" if hand == "L" else "RIGHTY"
    hand_color = (255, 180, 0) if hand == "L" else (0, 210, 255)
    cv2.putText(disp, f"Hand: {hand_label}  (L = Lefty, Y = Righty)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, hand_color, 2)
    cv2.putText(disp, "Click = move EoA (quadrant U)  |  D = drop test  |  H = home  |  R = refresh  |  Q = quit",
                (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 210, 255), 2)

    return disp


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 70)
    print("  CIB — Quadrant-Based U Selection Test")
    print("=" * 70)

    print(f"""
  RIGHTY: TOP_U (TL,TR) = {RIGHTY_TOP_U}   BOTTOM_U (BL,BR) = {RIGHTY_BOTTOM_U}
  LEFTY:  TOP_U (TL,TR) = {LEFTY_TOP_U}   BOTTOM_U (BL,BR) = {LEFTY_BOTTOM_U}
  Starts in RIGHTY -- press L to switch to Lefty, Y to switch back.

  Uses calibration_v3_tooloffset_server.prg (MOVE_U, Tool 1 active).
  CHECKLIST:
    - calibration_v3_tooloffset_server.prg running in RC+ (F5)
    - eoa_live_feed.py open in a second terminal
""")

    if not os.path.exists(H_PATH):
        print(f"  [ERROR] {H_PATH} not found.")
        sys.exit(1)
    H = np.load(H_PATH)
    print(f"  Loaded H matrix: {H_PATH}")

    sock = robot_connect()

    print()
    frame = grab_overhead()

    scale  = DISPLAY_WIDTH / frame.shape[1]
    clicks: list[tuple[int, int, float, float, float, str]] = []
    busy   = [False]
    hand_state = ["R"]   # mutable so on_click/key loop can both read+write it

    WIN = "Quadrant U Select — Click to move EoA"

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if busy[0]:
            print("  [WAIT] still moving ...")
            return

        full_x = x / scale
        full_y = y / scale
        rx, ry = pixel_to_robot(full_x, full_y, H)
        hand = hand_state[0]
        quadrant, u = quadrant_and_u(full_x, full_y, frame.shape[1], frame.shape[0], hand)

        print(f"\n  Click -> {quadrant}  display({x},{y})"
              f"  target robot({rx:.2f}, {ry:.2f}) mm  U={u:.1f}  hand={hand}")
        print("  Sending MOVE_U ...")

        busy[0] = True
        try:
            ax, ay, au = robot_move_u(sock, rx, ry, u, hand)
            print(f"  Robot at ({ax:.2f}, {ay:.2f}) mm  U={au:.2f}")
            print("  Watch eoa_live_feed.py — EoA crosshair should be on the clicked spot")
            clicks.append((x, y, rx, ry, u, quadrant))
            disp = build_display(frame, clicks, scale, hand_state[0])
            cv2.imshow(WIN, disp)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        busy[0] = False

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(frame.shape[0] * scale))
    cv2.setMouseCallback(WIN, on_click)

    print("  Overhead image open. Click points in each quadrant.\n")

    nonlocal_frame = [frame]

    while True:
        disp = build_display(nonlocal_frame[0], clicks, scale, hand_state[0])
        cv2.imshow(WIN, disp)
        key = cv2.waitKey(100) & 0xFF

        if key in (ord("q"), ord("Q"), 27):
            break

        if key in (ord("l"), ord("L")):
            hand_state[0] = "L"
            print("\n  Switched to LEFTY quadrant U's "
                  f"(top={LEFTY_TOP_U}, bottom={LEFTY_BOTTOM_U}).")
            continue

        if key in (ord("y"), ord("Y")):
            hand_state[0] = "R"
            print("\n  Switched to RIGHTY quadrant U's "
                  f"(top={RIGHTY_TOP_U}, bottom={RIGHTY_BOTTOM_U}).")
            continue

        if key in (ord("d"), ord("D")):
            if busy[0]:
                print("  [WAIT] robot busy, can't drop-test now")
                continue
            print("\n  Drop test -> Station 3 H12, Lefty, U=200.023 ...")
            busy[0] = True
            try:
                robot_drop_test(sock)
                print("  At Station 3 H12. Tool restored to 1 -- quadrant clicks still work.")
            except Exception as exc:
                print(f"  [ERROR] {exc}")
            busy[0] = False
            continue

        if key in (ord("h"), ord("H")):
            if busy[0]:
                print("  [WAIT] robot busy, can't home now")
                continue
            busy[0] = True
            robot_home(sock)
            busy[0] = False
            continue

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

    print("\n  Closing connection — robot NOT homed, staying in position.")
    try:
        send_cmd(sock, "BYE")
    except Exception:
        pass
    sock.close()

    print()
    if clicks:
        print("  Clicks recorded this session:")
        print(f"  {'Quadrant':<10} {'U':>8} {'Robot X':>10} {'Robot Y':>10}")
        print("  " + "-" * 42)
        for dx, dy, rx, ry, u, q in clicks:
            print(f"  {q:<10} {u:>8.1f} {rx:>10.2f} {ry:>10.2f}")
    print("\n  Done.")


if __name__ == "__main__":
    main()
