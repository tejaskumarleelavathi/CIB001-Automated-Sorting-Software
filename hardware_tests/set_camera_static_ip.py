"""
set_camera_static_ip.py -- Set persistent static IP on Lucid cameras.

Run this ONCE. After it completes, cameras always boot at the correct IP.
No more force_camera_ip.py needed at the start of each session.

Usage:
    python hardware_tests/set_camera_static_ip.py
"""

import time
import ipaddress
from arena_api.system import system  # type: ignore[import]


def _ip_to_int(ip: str) -> int:
    """Convert dotted IP string to 32-bit integer (Arena SDK format)."""
    return int(ipaddress.ip_address(ip))

# Target configuration -- matches machine_config.yaml
CAMERAS = {
    "253102168": "172.16.9.21",   # Overhead camera (Lucid TRI245S-CC)
    "253102169": "172.16.9.22",   # EoA camera on robot arm (Lucid TRI245S-CC)
}
SUBNET  = "255.255.255.0"
GATEWAY = "172.16.9.1"


def _wait_for_devices(timeout_s: float = 8.0):
    print("Discovering cameras ...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = system.device_infos
        if infos:
            return list(infos)
        time.sleep(0.5)
    return list(system.device_infos)


def set_persistent_ip(serial: str, target_ip: str) -> bool:
    """Open camera by serial and write persistent IP to its flash memory."""
    # Find the device info for this serial
    infos = system.device_infos
    match = [i for i in infos if str(i.get("serial", "")) == serial]
    if not match:
        print(f"  [SKIP] Serial {serial} not found in current device list.")
        return False

    info = match[0]
    current_ip = info.get("ip", "?")
    print(f"  Opening serial {serial}  (current IP: {current_ip}) ...")

    try:
        devices = system.create_device(device_infos=info)
        dev = devices[0]
        nm = dev.nodemap

        # Write persistent IP, subnet, gateway as 32-bit integers
        nm["GevPersistentIPAddress"].value      = _ip_to_int(target_ip)
        nm["GevPersistentSubnetMask"].value     = _ip_to_int(SUBNET)
        nm["GevPersistentDefaultGateway"].value = _ip_to_int(GATEWAY)

        # Enable persistent IP mode — camera automatically disables DHCP/LLA
        nm["GevCurrentIPConfigurationPersistentIP"].value = True

        # Save to UserSet1 so it survives power cycles
        nm["UserSetSelector"].value = "UserSet1"
        nm["UserSetSave"].execute()

        # Set UserSet1 as the default to load on boot
        nm["UserSetDefault"].value = "UserSet1"

        print(f"  [OK] Serial {serial} -> {target_ip} saved to UserSet1.")
        system.destroy_device(dev)
        return True

    except Exception as exc:
        print(f"  [FAIL] Serial {serial}: {exc}")
        return False


def main():
    infos = _wait_for_devices()
    if not infos:
        print("No cameras found. Check: cameras powered, cabled, PoE switch on.")
        return

    print(f"Found {len(infos)} camera(s):")
    for info in infos:
        print(f"  serial={info.get('serial')}  ip={info.get('ip')}  mac={info.get('mac')}")

    print()
    results = {}
    for serial, target_ip in CAMERAS.items():
        print(f"Setting serial {serial} -> {target_ip} ...")
        results[serial] = set_persistent_ip(serial, target_ip)
        time.sleep(1)

    print()
    print("Results:")
    for serial, ok in results.items():
        status = "DONE" if ok else "FAILED/SKIPPED"
        print(f"  {serial} -> {CAMERAS[serial]}  [{status}]")

    print()
    print("Power-cycle both cameras now.")
    print("After reboot they will always come up at the correct IP.")
    print("You will never need to run force_camera_ip.py again.")


if __name__ == "__main__":
    main()
