"""
test_sort_server_pick.py — manual test harness for sort_server.prg's
MOVE_IMAGE_U + PICK/PICK_U (real aspirate)
=============================================================================

Talks to the ACTUAL production robot program (sort_server.prg) using its
real command names — not a parallel test-only protocol. This is the manual,
one-step-at-a-time way to validate the new Tool 2 / pick wiring BEFORE
trusting the fully automatic orchestrator cycle (main.py) with it.

Click anywhere on the overhead image -> EoA moves there via MOVE_IMAGE_U
(quadrant U picked automatically — same logic main.py uses). Press P ->
pipette moves to that SAME target via PICK_U and performs a REAL aspirate
(Output 4 down -> confirm Input 22 -> motor EX G2 -> Output 4 up -> confirm
Input 20) — this actually attempts to pick up whatever is at that spot.

BEFORE RUNNING
  1. Load sort_server.prg in RC+, press F5.
  2. Run eoa_live_feed.py in a second terminal.
  3. Have a real (or disposable/test) specimen in the dish for the pick test —
     this performs a genuine aspirate, not a touch-only test.

Controls:
  Left-click  -> EoA moves there (MOVE_IMAGE_U, quadrant U auto-selected)
  P           -> pipette moves to the LAST clicked spot and ASPIRATES (PICK_U)
  R           -> retake overhead snapshot
  Q / Esc     -> home robot, quit

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/test_sort_server_pick.py
"""

import ctypes
import os
import socket
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Quadrant U — must match config/machine_config.yaml robot.u_top_deg / u_bottom_deg ──
TOP_U    = 209.0
BOTTOM_U = 251.0
QUADRANT_SPLIT_Y_MM = 298.4   # must match config/machine_config.yaml robot.quadrant_split_y_mm

# ── Settings ──────────────────────────────────────────────────────────────────

ROBOT_IP         = "172.16.9.15"
ROBOT_PORT       = 2000
ROBOT_TIMEOUT    = 30.0

OVERHEAD_SERIAL      = "253102168"
OVERHEAD_EXPOSURE_US = 60000
OVERHEAD_GAIN_DB     = 0.0

H_PATH        = os.path.join(os.path.dirname(__file__), "..", "camera_calibration",
                             "output", "calibration_overhead_tooloffset.npy")
DISPLAY_WIDTH = 1400

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


def send_cmd(s: socket.socket, cmd: str, timeout: float = ROBOT_TIMEOUT) -> str:
    s.settimeout(timeout)
    s.sendall((cmd + "\r\n").encode("ascii"))
    return _recv_line(s)


def robot_move_image_u(s: socket.socket, rx: float, ry: float, u: float) -> tuple[float, float]:
    resp = send_cmd(s, f"MOVE_IMAGE_U {rx:.3f},{ry:.3f},{u:.3f}")
    if not resp.startswith("OK IMAGE_POSITION"):
        raise RuntimeError(f"MOVE_IMAGE_U failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


def robot_pick_u(s: socket.socket, rx: float, ry: float, u: float) -> tuple[float, float]:
    # PICK_U blocks through the full aspirate sequence — give it a long timeout.
    resp = send_cmd(s, f"PICK_U {rx:.3f},{ry:.3f},{u:.3f}", timeout=120.0)
    if not resp.startswith("OK PICKED"):
        raise RuntimeError(f"PICK_U failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


def robot_drop_u(s: socket.socket, rx: float, ry: float, u: float) -> tuple[float, float]:
    # DROP_U blocks through the full dispense sequence — give it a long timeout.
    resp = send_cmd(s, f"DROP_U {rx:.3f},{ry:.3f},{u:.3f}", timeout=120.0)
    if not resp.startswith("OK DROPPED"):
        raise RuntimeError(f"DROP_U failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


def robot_home(s: socket.socket) -> None:
    print("  Homing robot ...")
    try:
        send_cmd(s, "HOME", timeout=60.0)
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


def quadrant_and_u(robot_y: float) -> tuple[str, float]:
    return ("TOP", TOP_U) if robot_y >= QUADRANT_SPLIT_Y_MM else ("BOTTOM", BOTTOM_U)


# ── Display ───────────────────────────────────────────────────────────────────

def build_display(frame: np.ndarray, clicks: list, scale: float, last_target) -> np.ndarray:
    dh = int(frame.shape[0] * scale)
    dw = int(frame.shape[1] * scale)
    disp = cv2.resize(frame, (dw, dh)).copy()

    for fx, fy, label in QUADRANT_LABELS:
        lx, ly = int(dw * fx), int(dh * fy)
        cv2.putText(disp, label, (lx - 20, ly),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 200, 255), 3)

    cv2.line(disp, (dw // 2, 0), (dw // 2, dh), (80, 80, 80), 1)
    cv2.line(disp, (0, dh // 2), (dw, dh // 2), (80, 80, 80), 1)

    for dx, dy, rx, ry, u in clicks:
        cv2.circle(disp, (dx, dy), 10, (0, 255, 0), -1)
        cv2.putText(disp, f"U={u:.0f}", (dx + 12, dy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    cv2.rectangle(disp, (0, 0), (dw, 56), (20, 20, 20), -1)
    cv2.putText(disp, "Click=MOVE_IMAGE_U  |  P=PICK_U (aspirate)  |  D=DROP_U (dispense)  |  R=refresh  |  Q=quit",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 210, 255), 2)
    if last_target is not None:
        cv2.putText(disp, f"Last target: ({last_target[0]:.2f}, {last_target[1]:.2f}) mm  U={last_target[2]:.0f}",
                    (10, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)

    return disp


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 70)
    print("  sort_server.prg manual test — MOVE_IMAGE_U + PICK_U (real aspirate)")
    print("=" * 70)
    print("""
  Click a point -> EoA moves there (MOVE_IMAGE_U, quadrant U auto-selected).
  Press P -> pipette moves to that SAME point and ASPIRATES FOR REAL
  (Output 4 down -> Input 22 confirms -> motor -> Output 4 up -> Input 20
  confirms). This actually attempts to pick up whatever is there.

  CHECKLIST:
    - sort_server.prg running in RC+ (F5)
    - eoa_live_feed.py open in a second terminal
    - Real/disposable specimen in the dish if you're testing P
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
    clicks: list[tuple[int, int, float, float, float]] = []
    busy   = [False]
    last_target = [None]   # (rx, ry, u)

    WIN = "sort_server.prg test — Click=MOVE_IMAGE_U, P=PICK_U"

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if busy[0]:
            print("  [WAIT] still moving ...")
            return

        full_x = x / scale
        full_y = y / scale
        rx, ry = pixel_to_robot(full_x, full_y, H)
        quadrant, u = quadrant_and_u(ry)

        print(f"\n  Click -> {quadrant}  target robot({rx:.2f}, {ry:.2f}) mm  U={u:.1f}")
        print("  Sending MOVE_IMAGE_U ...")

        busy[0] = True
        try:
            ax, ay = robot_move_image_u(sock, rx, ry, u)
            print(f"  EoA at ({ax:.2f}, {ay:.2f}) mm")
            last_target[0] = (rx, ry, u)
            clicks.append((x, y, rx, ry, u))
            disp = build_display(frame, clicks, scale, last_target[0])
            cv2.imshow(WIN, disp)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        busy[0] = False

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(frame.shape[0] * scale))
    cv2.setMouseCallback(WIN, on_click)

    print("  Overhead image open. Click a point, then P to test the real pick.\n")

    nonlocal_frame = [frame]

    while True:
        disp = build_display(nonlocal_frame[0], clicks, scale, last_target[0])
        cv2.imshow(WIN, disp)
        key = cv2.waitKey(100) & 0xFF

        if key in (ord("q"), ord("Q"), 27):
            break

        if key in (ord("p"), ord("P")):
            if busy[0]:
                print("  [WAIT] robot busy ...")
                continue
            if last_target[0] is None:
                print("  [INFO] Click a point first.")
                continue
            rx, ry, u = last_target[0]
            print(f"\n  Sending PICK_U to ({rx:.2f}, {ry:.2f}) mm  U={u:.1f} ...")
            print("  (blocks through the full aspirate sequence — may take a few seconds)")
            busy[0] = True
            try:
                px, py = robot_pick_u(sock, rx, ry, u)
                print(f"  PICKED at ({px:.2f}, {py:.2f}) mm — check the needle for the specimen.")
            except Exception as exc:
                print(f"  [ERROR] {exc}")
            busy[0] = False

        if key in (ord("d"), ord("D")):
            if busy[0]:
                print("  [WAIT] robot busy ...")
                continue
            if last_target[0] is None:
                print("  [INFO] Click a point first (that's where it will be released).")
                continue
            rx, ry, u = last_target[0]
            print(f"\n  Sending DROP_U to ({rx:.2f}, {ry:.2f}) mm  U={u:.1f} ...")
            print("  (blocks through the full dispense sequence — may take a few seconds)")
            busy[0] = True
            try:
                px, py = robot_drop_u(sock, rx, ry, u)
                print(f"  DROPPED at ({px:.2f}, {py:.2f}) mm.")
            except Exception as exc:
                print(f"  [ERROR] {exc}")
            busy[0] = False

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
    try:
        send_cmd(sock, "BYE")
    except Exception:
        pass
    sock.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
