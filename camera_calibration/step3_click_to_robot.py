"""
STEP 3 (interactive) — Click any point in the overhead camera live feed,
robot moves EoA there using the calibrated homography H.

This proves the pixel->robot conversion works end-to-end.

Controls (overhead camera window):
    Left-click  ->  robot moves EoA to that pixel location
    Q           ->  quit

BEFORE RUNNING:
  - calibration_server.prg is running in RC+ (F5).
  - Robot is away from the dish so it can move freely.
  - Run eoa_live_feed.py in a separate window to see where the EoA lands.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step3_click_to_robot.py
"""

import ctypes
import os
import sys
import socket
import time
import numpy as np
import cv2

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_DIR  = os.path.join(os.path.dirname(__file__), "output")
H_PATH   = os.path.join(OUT_DIR, "calibration_overhead.npy")

# ── Camera ────────────────────────────────────────────────────────────────────
OVERHEAD_SERIAL  = "253102168"
DISPLAY_WIDTH    = 1400    # scale down for display

# ── Robot ─────────────────────────────────────────────────────────────────────
ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 15.0


# ── Robot helpers ─────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((ROBOT_IP, ROBOT_PORT))
    return s

def recv_line(s: socket.socket) -> str:
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
    return recv_line(s)

def robot_moveto(s: socket.socket, rx: float, ry: float) -> tuple:
    resp = send_cmd(s, f"MOVETO {rx:.3f},{ry:.3f}")
    if not resp.startswith("OK MOVETO"):
        raise RuntimeError(f"MOVETO failed: {resp!r}")
    parts = resp.split()
    return float(parts[2]), float(parts[3])


# ── Homography ────────────────────────────────────────────────────────────────

def pixel_to_robot(px: float, py: float, H: np.ndarray) -> tuple:
    pt     = np.array([[[px, py]]], dtype=np.float32)
    result = cv2.perspectiveTransform(pt, H)
    return float(result[0][0][0]), float(result[0][0][1])


# ── Camera capture ────────────────────────────────────────────────────────────

def grab_overhead_frame(system) -> tuple:
    """Grab one frame from overhead camera. Returns (frame, scale_factor)."""
    infos = list(system.device_infos)
    info  = next((i for i in infos
                  if str(i.get("serial","")) == OVERHEAD_SERIAL), None)
    if info is None:
        raise RuntimeError(f"Overhead camera {OVERHEAD_SERIAL} not found.")

    devices = system.create_device(device_infos=info)
    device  = devices[0]

    nodemap = device.nodemap
    try: nodemap["PixelFormat"].value  = "BGR8"
    except Exception: pass
    try: nodemap["ExposureAuto"].value = "Continuous"
    except Exception: pass
    try: nodemap["GainAuto"].value     = "Continuous"
    except Exception: pass

    device.start_stream(2)
    for _ in range(10):
        buf = device.get_buffer(timeout=5000)
        device.requeue_buffer(buf)

    buf   = device.get_buffer(timeout=5000)
    bpp   = buf.bits_per_pixel // 8
    h, w  = buf.height, buf.width
    total = h * w * bpp
    raw   = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
    frame = np.ctypeslib.as_array(raw).copy().reshape(h, w, bpp)
    device.requeue_buffer(buf)
    device.stop_stream()
    system.destroy_device()

    return frame, w, h


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("CIB Calibration — Click-to-Robot verification")
    print("=" * 60)

    # Load H
    if not os.path.exists(H_PATH):
        print(f"ERROR: No H matrix at {H_PATH}. Run step2 first.")
        sys.exit(1)
    H = np.load(H_PATH)
    print(f"Loaded H from: {H_PATH}")

    # Connect to robot
    print(f"Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    try:
        sock = robot_connect()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    resp = send_cmd(sock, "STATUS")
    if "READY" not in resp:
        print(f"Unexpected STATUS: {resp}")
        sys.exit(1)
    print("Robot ready.")

    # Connect to overhead camera and grab one frame
    print("\nCapturing overhead image ...")
    try:
        from arena_api.system import system
    except ImportError:
        print("arena_api not found.")
        sys.exit(1)

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if list(system.device_infos):
            break
        time.sleep(0.5)

    try:
        frame, full_w, full_h = grab_overhead_frame(system)
    except Exception as e:
        print(f"Camera error: {e}")
        sys.exit(1)

    print(f"Captured: {full_w}x{full_h} px")

    # Scale for display
    scale   = DISPLAY_WIDTH / full_w
    disp_w  = int(full_w * scale)
    disp_h  = int(full_h * scale)
    display = cv2.resize(frame, (disp_w, disp_h)).copy()

    cv2.putText(display, "LEFT-CLICK to send robot there.  Q to quit.",
                (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)

    click_state = {"busy": False}

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if click_state["busy"]:
            print("  (still moving, please wait...)")
            return

        # Scale display pixel -> full resolution pixel
        full_x = x / scale
        full_y = y / scale

        # Pixel -> robot XY via H
        rx, ry = pixel_to_robot(full_x, full_y, H)

        print(f"\n  Clicked display ({x},{y})  "
              f"-> full res ({full_x:.1f},{full_y:.1f})  "
              f"-> robot ({rx:.3f},{ry:.3f}) mm")

        # Draw dot on display
        cv2.circle(display, (x, y), 10, (0, 255, 0), -1)
        cv2.putText(display, f"X={rx:.1f} Y={ry:.1f}",
                    (x + 14, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 0), 2)
        cv2.imshow(WIN, display)

        # Send to robot
        click_state["busy"] = True
        try:
            ax, ay = robot_moveto(sock, rx, ry)
            print(f"  Robot reached ({ax:.3f},{ay:.3f}) mm")
        except Exception as e:
            print(f"  MOVETO failed: {e}")
        click_state["busy"] = False

    WIN = "Overhead — click any point, robot goes there.  Q=quit"
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WIN, on_click)
    cv2.imshow(WIN, display)

    print("\nOverhead image is open.")
    print("Click any point -> robot EoA moves there.")
    print("Watch eoa_live_feed.py to see where the crosshair lands.")
    print("Press Q to quit.\n")

    while True:
        key = cv2.waitKey(50) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break

    cv2.destroyAllWindows()
    send_cmd(sock, "BYE")
    sock.close()
    print("Done.")


if __name__ == "__main__":
    main()
