"""
check_hardware.py — Run this at the start of any session.

Tells you exactly what is reachable and what is not:
  - Subnet scan (ping all 172.16.9.1-30)
  - Camera discovery + test capture from every camera found
  - Backlight (DCS-100E) reachability check

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/check_hardware.py

NOTE: Run from CIB_Software root, or cd into hardware_tests first.
"""

import socket
import subprocess
import sys
import time
import ctypes
from pathlib import Path

# ── known devices on 172.16.9.x ─────────────────────────────────────────────
KNOWN_DEVICES = {
    1:  "Ewon gateway",
    2:  "Dev environment gateway",
    10: "HMI Computer",
    11: "PLC (AutomationDirect P1-622)",
    12: "Safety Controller (Sick Flexi)",
    15: "Robot (Epson T3-B)",
    16: "Motor / Syringe Pump (Novanta M-Drive)",
    20: "Backlight controller (DCS-100E)",
    21: "Camera 1 (forced IP — Lucid TRI245S-C)",
    22: "Camera 2 (forced IP — Lucid EoA, if connected)",
}
SCAN_RANGE   = range(1, 31)
SUBNET       = "172.16.9"
DCS_IP       = "172.16.9.20"
DCS_PORT     = 777


# ── 1. Subnet ping scan ──────────────────────────────────────────────────────

def ping_scan() -> dict[int, bool]:
    print("=" * 60)
    print("SUBNET SCAN  172.16.9.1 – 172.16.9.30")
    print("=" * 60)
    alive = {}
    for i in SCAN_RANGE:
        ip = f"{SUBNET}.{i}"
        result = subprocess.run(
            ["ping", "-n", "1", "-w", "500", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        up = result.returncode == 0
        alive[i] = up
        label = KNOWN_DEVICES.get(i, "")
        status = "UP  " if up else "    "
        print(f"  {status} 172.16.9.{i:<4}  {label}")
    print()
    return alive


# ── 2. Backlight reachability ────────────────────────────────────────────────

def check_backlight() -> None:
    print("=" * 60)
    print("BACKLIGHT  DCS-100E  172.16.9.20:777")
    print("=" * 60)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect((DCS_IP, DCS_PORT))
        s.sendall(b"*IDN?;")
        time.sleep(0.2)
        try:
            resp = s.recv(1024).decode("ascii", errors="replace").strip()
        except socket.timeout:
            resp = "(no response)"
        s.sendall(b"DISCONNECT;")
        s.close()
        print(f"  OK   Connected.  IDN response: {resp}")
    except Exception as e:
        print(f"  FAIL  Cannot reach DCS-100E: {e}")
    print()


# ── 3. Camera discovery + test capture ──────────────────────────────────────

def check_cameras() -> None:
    print("=" * 60)
    print("CAMERAS  (Lucid GigE Vision via arena_api)")
    print("=" * 60)

    try:
        from arena_api.system import system
        import numpy as np
        import cv2
    except ImportError as e:
        print(f"  FAIL  Import error: {e}")
        print("  Install: pip install <Arena SDK>\\ArenaPy\\arena_api-*.whl")
        print()
        return

    # Discovery
    print("  Scanning (5 s) ...")
    deadline = time.monotonic() + 5.0
    infos = []
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            break
        time.sleep(0.5)

    if not infos:
        print("  No cameras found at all.")
        print("  Check: PoE switch on, cameras powered, GigE cable connected.")
        print()
        return

    print(f"  Found {len(infos)} camera(s):\n")

    for i, info in enumerate(infos):
        serial  = info.get("serial", "?")
        ip      = info.get("ip", "?")
        mac     = info.get("mac", "?")
        model   = info.get("model", "?")
        print(f"  Camera {i}: {model}  serial={serial}  ip={ip}  mac={mac}")

        # Force to 172.16.9.21 + i if not already there
        target_ip = f"172.16.9.{21 + i}"
        if ip != target_ip:
            print(f"    Forcing IP {ip} -> {target_ip} ...")
            forced = dict(info)
            forced["ip"]             = target_ip
            forced["subnetmask"]     = "255.255.255.0"
            forced["defaultgateway"] = "172.16.9.1"
            system.force_ip(forced)
            time.sleep(3)
            # re-discover
            infos2 = list(system.device_infos)
            info = next((d for d in infos2 if d.get("serial") == serial), info)
            ip = info.get("ip", target_ip)
            print(f"    IP is now: {ip}")

        # Try to open + capture
        print(f"    Opening camera ...")
        try:
            dev = system.create_device(device_infos=info)[0]
            try:
                nm = dev.nodemap
                try:
                    nm["PixelFormat"].value = "Mono8"
                except Exception:
                    pass

                dev.start_stream(2)
                buf = dev.get_buffer(timeout=5000)
                bpp = buf.bits_per_pixel // 8
                nb  = buf.width * buf.height * bpp
                raw = (ctypes.c_uint8 * nb).from_address(ctypes.addressof(buf.pbytes))
                frame = np.ctypeslib.as_array(raw).reshape(buf.height, buf.width if bpp==1 else (buf.width, bpp)).copy()
                dev.requeue_buffer(buf)
                dev.stop_stream()

                mean_px = float(frame.mean())
                max_px  = int(frame.max())
                out     = Path(f"capture_check_cam{i}_serial{serial}.png")
                cv2.imwrite(str(out), frame)

                print(f"    CAPTURE OK  shape={frame.shape}  mean={mean_px:.1f}  max={max_px}")
                if mean_px < 1.0:
                    print(f"    WARNING: mean={mean_px:.2f} — sensor is nearly dark.")
                    print(f"             Check: lens cap off? Camera aimed at backlight?")
                else:
                    print(f"    Image looks good.")
                print(f"    Saved: {out.resolve()}")

            finally:
                system.destroy_device(dev)

        except Exception as e:
            print(f"    FAIL  Could not open/capture: {e}")

        print()


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ping_scan()
    check_backlight()
    check_cameras()
    print("=" * 60)
    print("Done. Review any FAIL or WARNING lines above.")
    print("=" * 60)
