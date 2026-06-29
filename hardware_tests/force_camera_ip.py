"""
force_camera_ip.py — Force Lucid camera(s) to a usable IP in the 172.16.9.x subnet.

When a Lucid camera can't find a DHCP server it falls back to a link-local
address (169.254.x.x).  That puts it on a different subnet from the VPN
Ethernet interface (172.16.9.50/24) and arena_api refuses to open it.

The GigE Vision ForceIP command uses a MAC-addressed broadcast, so it works
even across subnets.  This script:
  1. Discovers all cameras (via broadcast)
  2. Forces each one to the target IP(s) below
  3. Re-discovers to confirm
  4. Saves a PNG test capture from each camera that responds

Edit CAMERA_IPS below to assign IPs before running.

Usage:
    python force_camera_ip.py
"""

import time
import ctypes
from pathlib import Path

from arena_api.system import system  # type: ignore[import]
import numpy as np

# ---------------------------------------------------------------------------
# Target IPs — assign one per camera.
# Cameras are on the PoE switch; .21 and .22 are free slots on 172.16.9.x.
# Fill in MAC addresses after first discovery run below, or leave empty
# to auto-assign sequentially.
# ---------------------------------------------------------------------------
TARGET_SUBNET   = "255.255.255.0"
TARGET_GATEWAY  = "172.16.9.1"

# Serial → target IP mapping (matches machine_config.yaml).
# This is the correct way to assign — by serial, not discovery order.
SERIAL_TO_IP: dict[str, str] = {
    "253102168": "172.16.9.21",   # Overhead camera (Lucid TRI245S-CC)
    "253102169": "172.16.9.22",   # EoA camera on robot arm (Lucid TRI245S-CC)
}
AUTO_START_IP_SUFFIX = 21   # fallback if serial not in SERIAL_TO_IP


def _wait_for_cameras(timeout_s: float = 5.0) -> list:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = system.device_infos
        if infos:
            return list(infos)
        time.sleep(0.5)
    return list(system.device_infos)


def force_all_cameras() -> None:
    print("\nDiscovering cameras ...")
    infos = _wait_for_cameras(6.0)

    if not infos:
        print("[ERROR] No cameras found on the network.")
        print("Check: cameras powered, cabled, and PoE switch on.")
        return

    print(f"Found {len(infos)} camera(s):")
    for i, info in enumerate(infos):
        ip  = info.get("ip", "?")
        mac = info.get("mac", "?")
        sn  = info.get("serial", "?")
        print(f"  [{i}] serial={sn}  ip={ip}  mac={mac}")

    # Determine target IPs — by serial number, not discovery order
    assignments = []
    fallback_idx = 0
    for info in infos:
        serial = info.get("serial", "")
        if serial in SERIAL_TO_IP:
            target_ip = SERIAL_TO_IP[serial]
        else:
            target_ip = f"172.16.9.{AUTO_START_IP_SUFFIX + fallback_idx}"
            fallback_idx += 1

        assignments.append((info, target_ip))

    # Force IPs
    print("\nForcing IPs ...")
    for info, target_ip in assignments:
        mac = info.get("mac", "?")
        cur_ip = info.get("ip", "?")
        print(f"  {mac}  {cur_ip} -> {target_ip}")

        forced_info = dict(info)
        forced_info["ip"]             = target_ip
        forced_info["subnetmask"]     = TARGET_SUBNET
        forced_info["defaultgateway"] = TARGET_GATEWAY

        system.force_ip(forced_info)

    print("\nWaiting 4 s for cameras to apply new IPs ...")
    time.sleep(4)

    # Re-discover (accessing device_infos re-enumerates)
    print("Re-discovering ...")
    new_infos = _wait_for_cameras(6.0)

    if not new_infos:
        print("[WARN] No cameras found after force — they may still be rebooting.")
        print("  Re-run this script, or re-run test_camera.py discover in a few seconds.")
        return

    print(f"\nCameras after force ({len(new_infos)}):")
    for i, info in enumerate(new_infos):
        ip  = info.get("ip", "?")
        mac = info.get("mac", "?")
        sn  = info.get("serial", "?")
        print(f"  [{i}] serial={sn}  ip={ip}  mac={mac}")

    # Try a test capture from each
    print("\nAttempting test captures ...")
    for i, info in enumerate(new_infos):
        serial = info.get("serial", "?")
        ip     = info.get("ip", "?")
        try:
            devices = system.create_device(device_infos=info)
            dev = devices[0]
            frame = _capture(dev)
            system.destroy_device(dev)
            out = Path(f"capture_cam{i}_serial{serial}.png")
            import cv2
            cv2.imwrite(str(out), frame)
            print(f"  [OK] Camera {i} (serial={serial}, ip={ip}) -> {out.resolve()}")
            print(f"       shape={frame.shape}  min={frame.min()}  max={frame.max()}")
        except Exception as e:
            print(f"  [FAIL] Camera {i} (serial={serial}, ip={ip}): {e}")

    print("\nDone.")
    print("Update OVERHEAD_SERIAL / END_OF_ARM_SERIAL in test_camera.py with the serials above.")


def _capture(device) -> np.ndarray:
    device.start_stream(2)   # OldestFirstOverwrite requires >= 2 buffers
    try:
        buffer = device.get_buffer(timeout=5000)
        bpp = buffer.bits_per_pixel // 8
        total_bytes = buffer.width * buffer.height * bpp
        raw = (ctypes.c_uint8 * total_bytes).from_address(
            ctypes.addressof(buffer.pbytes)
        )
        arr = np.ctypeslib.as_array(raw)
        if bpp == 1:
            frame = arr.reshape(buffer.height, buffer.width).copy()
        else:
            frame = arr.reshape(buffer.height, buffer.width, bpp).copy()
        device.requeue_buffer(buffer)
    finally:
        device.stop_stream()
    return frame


if __name__ == "__main__":
    force_all_cameras()
