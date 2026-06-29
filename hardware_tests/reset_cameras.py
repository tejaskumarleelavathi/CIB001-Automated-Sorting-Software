"""
reset_cameras.py — Force-release any stale arena_api device handles.

Run this between sessions when main.py was closed without SHUTDOWN and
the cameras are stuck with GC_ERR_TIMEOUT on the next startup.

Usage:
    python hardware_tests/reset_cameras.py
"""

import time
from arena_api.system import system  # type: ignore[import]

CAMERA_IPS = ["172.16.9.21", "172.16.9.22"]

print("Adding unicast discovery targets...")
for ip in CAMERA_IPS:
    system.add_unicast_discovery_device(ip)

print("Discovering cameras (5s)...")
time.sleep(1)
infos = list(system.device_infos)
print(f"Found {len(infos)} camera(s)")

if infos:
    print("Opening all devices to force-claim control channels...")
    try:
        devices = system.create_device(device_infos=infos)
        print(f"Opened {len(devices)} device(s)")
        for dev in devices:
            try:
                dev.stop_stream()
            except Exception:
                pass
        print("Destroying devices (releases GVCP control channels)...")
        system.destroy_device()
        print("Done — cameras are now free for the next session.")
    except Exception as e:
        print(f"create_device failed: {e}")
        print("Try unplugging the USB-Ethernet adapters for 5s, then re-plug.")
else:
    print("No cameras found. Check cables and power.")

time.sleep(1)
