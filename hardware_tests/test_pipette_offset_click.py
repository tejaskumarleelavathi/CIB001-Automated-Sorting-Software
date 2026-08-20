"""
test_pipette_offset_click.py — EoA vs Pipette tool offset sign test
=============================================================================

Click anywhere on the overhead image -> EoA camera moves there (visual
confirmation via eoa_live_feed.py). Press P -> PIPETTE moves to that SAME
target and extends down (Output 4 ON) so you can see by eye whether it
lands on the clicked spot. Press U to retract the pipette.

This tests David's CAD-measured camera-to-pipette offset (nominally 40mm /
0.5mm) — specifically the unresolved sign on the 40mm (cx) axis. The robot
program has Tool 2 set to the "positive X" candidate
(cx = camera_cx + 40 = 57.1876). If the pipette consistently lands offset
from the bug/target by roughly DOUBLE the expected amount, the sign is
wrong — edit TLSet 2 in pipette_offset_test_server.prg to
cx = 17.1876 - 40 = -22.8124, reload, and re-test.

Quadrant U selection is applied automatically (same as
test_quadrant_u_select.py) since which physical point lands where depends
on U for BOTH tools.

BEFORE RUNNING
  1. Load pipette_offset_test_server.prg in RC+, press F5.
  2. Run eoa_live_feed.py in a second terminal (to see the EoA crosshair).
  3. Fill in TOP_U / BOTTOM_U below if they change.

Controls:
  Left-click  -> EoA moves to that spot (retracts pipette first, for safety)
  P           -> Pipette moves to the LAST clicked spot, extends down (Output 4 ON)
  U           -> Retract pipette (Output 4 OFF), lift to safe Z
  R           -> retake overhead snapshot
  Q / Esc     -> retract pipette, home, quit

Usage:
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/test_pipette_offset_click.py
"""

import ctypes
import os
import socket
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Quadrant U (same as test_quadrant_u_select.py) ──────────────────────────
TOP_U    = 209.0   # U angle (deg) for TL + TR quadrants
BOTTOM_U = 251.0   # U angle (deg) for BL + BR quadrants

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
        print("  Make sure pipette_offset_test_server.prg is running in RC+ (F5).")
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


def robot_move_eoa(s: socket.socket, rx: float, ry: float, u: float) -> tuple[float, float]:
    resp = send_cmd(s, f"MOVE_EOA {rx:.3f},{ry:.3f},{u:.3f}")
    if not resp.startswith("OK EOA_POSITION"):
        raise RuntimeError(f"MOVE_EOA failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


def robot_move_pipette(s: socket.socket, rx: float, ry: float, u: float) -> tuple[float, float]:
    resp = send_cmd(s, f"MOVE_PIPETTE {rx:.3f},{ry:.3f},{u:.3f}")
    if not resp.startswith("OK PIPETTE_POSITION"):
        raise RuntimeError(f"MOVE_PIPETTE failed: {resp!r}")
    parts = resp.split()
    return float(parts[2].split(",")[0]), float(parts[2].split(",")[1])


def robot_pipette_up(s: socket.socket) -> None:
    try:
        resp = send_cmd(s, "PIPETTE_UP")
        if not resp.startswith("OK PIPETTE_UP"):
            print(f"  [WARN] PIPETTE_UP unexpected response: {resp!r}")
    except Exception as exc:
        print(f"  [WARN] PIPETTE_UP failed: {exc}")


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


def quadrant_and_u(full_x: float, full_y: float, img_w: int, img_h: int) -> tuple[str, float]:
    """Image-top half -> BOTTOM_U, image-bottom half -> TOP_U (camera is mounted
    so image-top is physically the dish's bottom — see test_quadrant_u_select.py)."""
    is_top_physical = full_y >= img_h / 2
    is_left = full_x < img_w / 2
    quadrant = ("TL" if is_left else "TR") if is_top_physical else ("BL" if is_left else "BR")
    u = TOP_U if is_top_physical else BOTTOM_U
    return quadrant, u


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

    for dx, dy, rx, ry, u, q in clicks:
        cv2.circle(disp, (dx, dy), 10, (0, 255, 0), -1)
        cv2.putText(disp, f"{q} U={u:.0f}",
                    (dx + 12, dy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    cv2.rectangle(disp, (0, 0), (dw, 56), (20, 20, 20), -1)
    cv2.putText(disp, "Click=EoA there  |  P=pipette down  |  U=pipette up  |  R=refresh  |  Q=quit",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 210, 255), 2)
    if last_target is not None:
        cv2.putText(disp, f"Last target: ({last_target[0]:.2f}, {last_target[1]:.2f}) mm  U={last_target[2]:.0f}",
                    (10, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)

    return disp


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 70)
    print("  Pipette Offset Sign Test — Tool 2 candidate: cx = camera_cx + 40")
    print("=" * 70)
    print("""
  Click a point -> EoA moves there (watch eoa_live_feed.py for confirmation).
  Press P -> pipette moves to the SAME point and extends down (Output 4 ON).
  Look at where the needle physically lands vs. the clicked spot.

  If it's off by roughly the FULL 40mm (not just residual mm noise), the
  sign is wrong -> edit TLSet 2 in pipette_offset_test_server.prg to
  cx = 17.1876 - 40 = -22.8124, reload, and re-test.

  CHECKLIST:
    - pipette_offset_test_server.prg running in RC+ (F5)
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
    last_target = [None]   # (rx, ry, u)

    WIN = "Pipette Offset Test — Click=EoA, P=pipette"

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if busy[0]:
            print("  [WAIT] still moving ...")
            return

        full_x = x / scale
        full_y = y / scale
        rx, ry = pixel_to_robot(full_x, full_y, H)
        quadrant, u = quadrant_and_u(full_x, full_y, frame.shape[1], frame.shape[0])

        print(f"\n  Click -> {quadrant}  target robot({rx:.2f}, {ry:.2f}) mm  U={u:.1f}")
        print("  Sending MOVE_EOA ...")

        busy[0] = True
        try:
            ax, ay = robot_move_eoa(sock, rx, ry, u)
            print(f"  EoA at ({ax:.2f}, {ay:.2f}) mm")
            last_target[0] = (rx, ry, u)
            clicks.append((x, y, rx, ry, u, quadrant))
            disp = build_display(frame, clicks, scale, last_target[0])
            cv2.imshow(WIN, disp)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        busy[0] = False

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, DISPLAY_WIDTH, int(frame.shape[0] * scale))
    cv2.setMouseCallback(WIN, on_click)

    print("  Overhead image open. Click a point, then press P to test the pipette.\n")

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
            print(f"\n  Sending MOVE_PIPETTE to ({rx:.2f}, {ry:.2f}) mm  U={u:.1f} ...")
            busy[0] = True
            try:
                px, py = robot_move_pipette(sock, rx, ry, u)
                print(f"  Pipette at ({px:.2f}, {py:.2f}) mm — look at the needle now.")
            except Exception as exc:
                print(f"  [ERROR] {exc}")
            busy[0] = False

        if key in (ord("u"), ord("U")):
            if busy[0]:
                print("  [WAIT] robot busy ...")
                continue
            print("\n  Retracting pipette ...")
            busy[0] = True
            robot_pipette_up(sock)
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
    robot_pipette_up(sock)
    robot_home(sock)
    try:
        send_cmd(sock, "BYE")
    except Exception:
        pass
    sock.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
