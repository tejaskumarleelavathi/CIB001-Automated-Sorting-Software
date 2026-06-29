"""
reset_camera_ip.py -- Remove persistent static IP from Lucid cameras.

The host Ethernet adapter has a 169.254.x.x/16 link-local address alongside
172.16.9.50/24. Arena_api binds cameras to the 169.254 interface during
discovery, so cameras at 172.16.9.x fail to open ("different subnet").
Cameras at 169.254.x.x work correctly because they match the host's /16.

This script temporarily moves each camera to a unique 169.254.x.x address
(no conflict), opens it, clears the persistent IP, then destroys cleanly.
After power-cycling both cameras they will boot at their natural LLA address
and the driver will connect to them directly — no force_ip needed.

Usage:
    python hardware_tests/reset_camera_ip.py
"""

import time
from arena_api.system import system  # type: ignore[import]

CAMERAS = {
    "253102168": "overhead",
    "253102169": "eoa",
}

# Temp LLA addresses — on the same /16 as the host's 169.254.84.78/16.
# Chosen far from the host address and from each other to avoid conflict.
TEMP_LLA = {
    "253102168": "169.254.1.168",
    "253102169": "169.254.1.169",
}


def _wait_for_serial(serial: str, timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for info in list(system.device_infos):
            if str(info.get("serial", "")) == serial:
                return info
        time.sleep(0.5)
    return None


def reset_to_lla(serial: str, label: str) -> bool:
    info = _wait_for_serial(serial, timeout_s=8.0)
    if info is None:
        print(f"  [SKIP] Serial {serial} ({label}) not found.")
        return False

    current_ip = info.get("ip", "?")
    temp_ip = TEMP_LLA[serial]
    print(f"  Serial {serial} ({label})  current IP: {current_ip}")

    # Force to a known LLA address so arena_api opens it on the correct
    # (169.254.x.x/16) interface.
    if current_ip != temp_ip:
        print(f"  force_ip -> {temp_ip} ...")
        forced = dict(info)
        forced["ip"]             = temp_ip
        forced["subnetmask"]     = "255.255.0.0"   # /16 to match host's LLA interface
        forced["defaultgateway"] = "0.0.0.0"
        system.force_ip(forced)
        print("  Waiting 6s for camera network stack to reboot ...")
        time.sleep(6.0)

        info = _wait_for_serial(serial, timeout_s=10.0)
        if info is None:
            print(f"  [FAIL] Camera did not reappear after force_ip.")
            return False
        print(f"  Camera at {info.get('ip')}. Opening ...")
    else:
        print(f"  Already at target. Opening ...")

    try:
        devices = system.create_device(device_infos=info)
        dev = devices[0]
        nm  = dev.nodemap

        nm["GevCurrentIPConfigurationPersistentIP"].value = False
        nm["UserSetSelector"].value = "UserSet1"
        nm["UserSetSave"].execute()
        nm["UserSetDefault"].value = "UserSet1"

        system.destroy_device(dev)
        print(f"  [OK] Persistent IP cleared on serial {serial} ({label}).")
        return True

    except Exception as exc:
        print(f"  [FAIL] Serial {serial} ({label}): {exc}")
        return False


def main():
    print("Discovering cameras ...")
    infos = []
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            break
        time.sleep(0.5)

    if not infos:
        print("No cameras found.")
        return

    print(f"Found {len(infos)} camera(s):")
    for i in infos:
        print(f"  serial={i.get('serial')}  ip={i.get('ip')}  mac={i.get('mac')}")
    print()

    for serial, label in CAMERAS.items():
        print(f"--- Resetting {serial} ({label}) ---")
        reset_to_lla(serial, label)
        print(f"Waiting 4s before next camera ...")
        time.sleep(4.0)
        print()

    print("Done. Power-cycle BOTH cameras now.")
    print("They will boot at a natural 169.254.x.x address.")
    print("The driver will find them by serial and connect directly.")


if __name__ == "__main__":
    main()
