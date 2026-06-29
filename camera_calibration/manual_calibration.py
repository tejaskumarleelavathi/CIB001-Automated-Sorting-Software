#!/usr/bin/env python3
"""
camera_calibration/manual_calibration.py
=========================================
Simple calibration using real bugs in the dish — no printed checkerboard needed.

Flow (repeat for 6+ bugs):
  1. Overhead snapshot shown — click a bug
  2. Jog robot with teach pendant until EoA camera centre is over that bug
  3. Press ENTER — script reads robot XY automatically via WHERE
  4. Pair (pixel_x, pixel_y) <-> (robot_x, robot_y) is recorded
  5. After 4+ pairs: homography H is computed and saved

BEFORE RUNNING:
  - Load calibration_server.prg in RC+ and press F5
  - Run eoa_live_feed.py in a separate window to see the EoA crosshair
  - Backlight ON, bugs in dish

Usage:
    python camera_calibration/manual_calibration.py
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Config ────────────────────────────────────────────────────────────────────
OVERHEAD_SERIAL = "253102168"
OVERHEAD_IP     = "172.16.9.21"
ROBOT_IP        = "172.16.9.15"
ROBOT_PORT      = 2000
ROBOT_TIMEOUT   = 10.0
DISPLAY_WIDTH   = 1400       # px — overhead display window width

OUT_DIR   = Path(__file__).parent / "output"
H_LOCAL   = OUT_DIR / "calibration_overhead.npy"
H_RUNTIME = Path(r"C:\LabAutomation\config\calibration_overhead.npy")
PAIRS_OUT = OUT_DIR / "calibration_pairs.json"

MIN_PAIRS = 4    # minimum to compute H
TARGET_PAIRS = 6 # recommended


# ── Robot helpers ─────────────────────────────────────────────────────────────

def robot_connect() -> socket.socket | None:
    """Try to connect to calibration_server.prg. Returns None if unavailable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(ROBOT_TIMEOUT)
        s.connect((ROBOT_IP, ROBOT_PORT))
        resp = _recv_line(s)   # drain welcome / first line if any
        # Send STATUS to confirm it's calibration_server.prg
        s.sendall(b"STATUS\r\n")
        resp = _recv_line(s)
        if "READY" in resp:
            print(f"  Robot connected ({resp})")
            return s
        s.close()
        return None
    except Exception as exc:
        print(f"  Robot not reachable ({exc}) — will ask for manual XY input.")
        return None


def _recv_line(s: socket.socket) -> str:
    buf = b""
    while True:
        ch = s.recv(1)
        if not ch:
            break
        buf += ch
        if ch == b"\n":
            break
    return buf.decode("ascii", errors="replace").strip()


def robot_where(s: socket.socket) -> tuple[float, float] | None:
    """Send WHERE, return (x, y) or None on failure."""
    try:
        s.sendall(b"WHERE\r\n")
        resp = _recv_line(s)
        # Expected: "OK WHERE x y z"
        parts = resp.split()
        if len(parts) >= 4 and "WHERE" in parts:
            idx = parts.index("WHERE")
            return float(parts[idx + 1]), float(parts[idx + 2])
        return None
    except Exception:
        return None


# ── Camera helpers ────────────────────────────────────────────────────────────

def grab_overhead(config_path: Path) -> tuple[np.ndarray, int, int] | None:
    """Capture one overhead frame. Returns (frame, width, height) or None."""
    try:
        from arena_api.system import system
    except ImportError:
        print("arena_api not available — cannot capture overhead frame.")
        return None

    # Load config for IP
    oh_ip = OVERHEAD_IP
    try:
        import yaml
        cfg = yaml.safe_load(config_path.read_text())
        oh_ip = cfg.get("overhead_camera", {}).get("ip", OVERHEAD_IP)
    except Exception:
        pass

    print("  Discovering cameras...")
    if oh_ip:
        try:
            system.add_unicast_discovery_device(oh_ip)
        except Exception:
            pass

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if any(str(i.get("serial", "")) == OVERHEAD_SERIAL for i in infos):
            break
        time.sleep(0.5)

    infos = list(system.device_infos)
    target = next((i for i in infos
                   if str(i.get("serial", "")) == OVERHEAD_SERIAL), None)
    if target is None:
        print(f"  Overhead camera {OVERHEAD_SERIAL} not found.")
        return None

    devices = system.create_device(device_infos=target)
    device  = devices[0]

    nm = device.nodemap
    try: nm["PixelFormat"].value  = "Mono8"
    except Exception: pass
    try: nm["ExposureAuto"].value = "Off"
    except Exception: pass
    try: nm["ExposureTime"].value = 3000.0
    except Exception: pass
    try: nm["GainAuto"].value = "Off"
    except Exception: pass

    tl = device.tl_stream_nodemap
    try: tl["StreamAutoNegotiatePacketSize"].value = False
    except Exception: pass
    try: tl["StreamPacketResendEnable"].value = True
    except Exception: pass

    device.start_stream(2)
    print("  Draining warmup frames...")
    for _ in range(10):
        buf = device.get_buffer(timeout=60_000)
        device.requeue_buffer(buf)

    buf = device.get_buffer(timeout=60_000)
    bpp    = int(buf.bits_per_pixel) // 8
    h, w   = int(buf.height), int(buf.width)
    total  = h * w * bpp
    raw    = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
    frame  = np.ctypeslib.as_array(raw).copy()
    frame  = frame.reshape(h, w) if bpp == 1 else frame.reshape(h, w, bpp)
    device.requeue_buffer(buf)

    device.stop_stream()
    system.destroy_device()

    if oh_ip:
        try: system.remove_unicast_discovery_device(oh_ip)
        except Exception: pass

    return frame, w, h


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("CIB Manual Calibration — click bugs, jog robot to match")
    print("=" * 60)
    print(f"""
BEFORE CONTINUING:
  1. calibration_server.prg running in RC+ (F5)
  2. eoa_live_feed.py open in a separate terminal (to see EoA crosshair)
  3. Backlight ON, bugs in dish
  4. Collect {TARGET_PAIRS}+ widely-spread bugs across the dish
""")

    # ── Robot connection ──────────────────────────────────────────────────────
    input("Press ENTER to connect to robot and camera...")
    sock = robot_connect()

    # ── Overhead snapshot ─────────────────────────────────────────────────────
    config_path = Path(__file__).parent.parent / "config" / "machine_config.yaml"
    prod_config = Path(r"C:\LabAutomation\config\machine_config.yaml")
    if prod_config.exists():
        config_path = prod_config

    print("\nCapturing overhead snapshot...")
    result = grab_overhead(config_path)
    if result is None:
        print("ERROR: Could not capture overhead image. Exiting.")
        if sock: sock.close()
        return

    frame, full_w, full_h = result
    print(f"  Captured {full_w}x{full_h}")

    # ── Display setup ─────────────────────────────────────────────────────────
    scale  = DISPLAY_WIDTH / full_w
    disp_w = int(full_w * scale)
    disp_h = int(full_h * scale)

    if frame.ndim == 2:
        display_base = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        display_base = frame.copy()
    display_base = cv2.resize(display_base, (disp_w, disp_h))

    pairs: list[dict] = []
    pending_click: list[tuple[int, int]] = []   # display coords of last click

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and not pending_click:
            pending_click.append((x, y))

    WIN = f"Overhead — click a bug  [{len(pairs)}/{TARGET_PAIRS} collected]  Q=quit"
    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WIN, on_click)

    print(f"\nOverhead window open.")
    print(f"Click a bug → jog robot EoA centre over it → press ENTER in terminal.")
    print(f"Collect {TARGET_PAIRS}+ bugs spread across the whole dish.\n")

    display = display_base.copy()
    cv2.putText(display,
                f"Click a bug  [{len(pairs)}/{TARGET_PAIRS}]  Q=quit",
                (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    cv2.imshow(WIN, display)

    while True:
        key = cv2.waitKey(50) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            print("Quit.")
            break

        if not pending_click:
            continue

        dx, dy = pending_click.pop()
        # Scale to full resolution
        px = dx / scale
        py = dy / scale

        print(f"\n--- Bug {len(pairs) + 1} ---")
        print(f"  Clicked pixel: ({px:.1f}, {py:.1f})")
        print(f"  Now jog the robot until the EoA crosshair is centred over that bug.")

        # Draw cyan circle on display at click position
        disp_copy = display_base.copy()
        for p in pairs:
            cx = int(p["pixel_x"] * scale)
            cy = int(p["pixel_y"] * scale)
            cv2.circle(disp_copy, (cx, cy), 12, (0, 200, 0), -1)
            cv2.putText(disp_copy,
                        f"{p['robot_x']:.1f},{p['robot_y']:.1f}",
                        (cx + 14, cy), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (0, 200, 0), 1)
        cv2.circle(disp_copy, (dx, dy), 14, (0, 255, 255), 2)
        cv2.circle(disp_copy, (dx, dy),  4, (0, 255, 255), -1)
        cv2.putText(disp_copy, "JOG HERE",
                    (dx + 16, dy), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 255), 2)
        cv2.putText(disp_copy,
                    f"Click a bug  [{len(pairs)}/{TARGET_PAIRS}]  Q=quit",
                    (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.imshow(WIN, disp_copy)
        cv2.waitKey(1)

        # Get robot XY
        rx, ry = None, None
        if sock:
            input("  Press ENTER when EoA crosshair is centred over the bug...")
            pos = robot_where(sock)
            if pos:
                rx, ry = pos
                print(f"  Robot WHERE: ({rx:.3f}, {ry:.3f}) mm")
            else:
                print("  WHERE failed — enter manually.")

        if rx is None or ry is None:
            try:
                raw = input("  Enter robot X,Y from pendant (e.g. 45.2,212.8): ").strip()
                parts = raw.replace(" ", "").split(",")
                rx, ry = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                print("  Invalid input — skipping this point.")
                display = disp_copy
                continue

        pairs.append({"pixel_x": px, "pixel_y": py,
                      "robot_x": rx, "robot_y": ry})
        print(f"  Recorded: pixel ({px:.1f},{py:.1f}) <-> robot ({rx:.3f},{ry:.3f})")
        print(f"  Total pairs: {len(pairs)}")

        # Update display — mark confirmed point green
        display = disp_copy.copy()
        cv2.circle(display, (dx, dy), 12, (0, 200, 0), -1)
        cv2.putText(display,
                    f"{rx:.1f},{ry:.1f}",
                    (dx + 14, dy), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 200, 0), 1)
        cv2.putText(display,
                    f"Click a bug  [{len(pairs)}/{TARGET_PAIRS}]  Q=quit",
                    (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.imshow(WIN, display)

        if len(pairs) >= TARGET_PAIRS:
            more = input(f"\n  {len(pairs)} pairs collected. Add more? (y/N): ").strip().lower()
            if more != "y":
                break

    cv2.destroyAllWindows()
    if sock:
        try:
            sock.sendall(b"BYE\r\n")
            sock.close()
        except Exception:
            pass

    # ── Compute homography ────────────────────────────────────────────────────
    if len(pairs) < MIN_PAIRS:
        print(f"\nNeed at least {MIN_PAIRS} pairs — only got {len(pairs)}. Exiting.")
        return

    print(f"\nComputing homography from {len(pairs)} pairs...")
    px_arr = np.array([(p["pixel_x"], p["pixel_y"]) for p in pairs], dtype=np.float32)
    rb_arr = np.array([(p["robot_x"],  p["robot_y"])  for p in pairs], dtype=np.float32)

    H, mask = cv2.findHomography(px_arr, rb_arr, cv2.RANSAC, 5.0)
    if H is None:
        print("ERROR: findHomography failed — points may be collinear or too clustered.")
        return

    inliers = int(mask.sum()) if mask is not None else len(pairs)
    print(f"  Inliers: {inliers}/{len(pairs)}")

    # Reprojection check
    print("\n  Reprojection check:")
    errors = []
    for p in pairs:
        pt   = np.array([[[p["pixel_x"], p["pixel_y"]]]], dtype=np.float32)
        pred = cv2.perspectiveTransform(pt, H)[0][0]
        err  = float(((pred[0] - p["robot_x"])**2 + (pred[1] - p["robot_y"])**2) ** 0.5)
        errors.append(err)
        print(f"    pixel ({p['pixel_x']:.0f},{p['pixel_y']:.0f})"
              f" -> predicted ({pred[0]:.2f},{pred[1]:.2f})"
              f" actual ({p['robot_x']:.2f},{p['robot_y']:.2f})"
              f"  err={err:.3f}mm")

    rmse = float(np.sqrt(np.mean(np.array(errors) ** 2)))
    print(f"\n  RMSE: {rmse:.3f} mm", end="  ")
    if rmse < 0.5:
        print("GOOD")
    elif rmse < 1.0:
        print("ACCEPTABLE")
    else:
        print("HIGH — try again with points spread more widely across the dish")

    # ── Save ──────────────────────────────────────────────────────────────────
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.save(str(H_LOCAL), H)
    print(f"\nSaved: {H_LOCAL}")

    try:
        H_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(H_LOCAL), str(H_RUNTIME))
        print(f"Copied to runtime: {H_RUNTIME}")
    except Exception as exc:
        print(f"Could not copy to {H_RUNTIME}: {exc}")
        print("Copy it manually after this script exits.")

    with open(PAIRS_OUT, "w") as f:
        json.dump({"rmse_mm": rmse, "pairs": pairs}, f, indent=2)
    print(f"Saved pairs: {PAIRS_OUT}")

    print("\n" + "=" * 60)
    if rmse < 1.0:
        print(f"CALIBRATION COMPLETE  (RMSE={rmse:.3f} mm)")
        print("Run camera_calibration/step3_click_to_robot.py to spot-check.")
    else:
        print(f"RMSE={rmse:.3f} mm — redo with points spread further apart.")
    print("=" * 60)


if __name__ == "__main__":
    main()
