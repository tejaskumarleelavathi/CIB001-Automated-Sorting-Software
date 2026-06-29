"""
set_camera_persistent_ip.py -- Set persistent static IPs on Lucid cameras.

After running this script and power-cycling both cameras, they will boot
directly at 172.16.9.21/22 on the main subnet. The driver's force_ip logic
will be bypassed entirely — create_device will succeed immediately.

Run this ONCE. After power-cycle, cameras will always use these IPs.

Usage:
    python hardware_tests/set_camera_persistent_ip.py

If a camera is currently at a 169.254.x.x address, the script will
force_ip it to a temp LLA first, open it, then set the persistent IP.
"""

import time
from arena_api.system import system  # type: ignore[import]

CAMERAS = {
    "253102168": {"label": "overhead", "target_ip": "172.16.9.21"},
    "253102169": {"label": "eoa",      "target_ip": "172.16.9.22"},
}

# Temp LLA addresses used to open cameras before setting persistent IP.
# Must be in /16 to match host's 169.254.84.78/16 interface.
TEMP_LLA = {
    "253102168": "169.254.3.168",
    "253102169": "169.254.3.169",
}

SUBNET    = "255.255.255.0"
GATEWAY   = "0.0.0.0"
LLA_SUBNET = "255.255.0.0"


def _wait_for_serial(serial: str, timeout_s: float = 12.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for info in list(system.device_infos):
            if str(info.get("serial", "")) == serial:
                return info
        time.sleep(0.5)
    return None


def set_persistent_ip(serial: str, label: str, target_ip: str) -> bool:
    print(f"--- {serial} ({label}) -> {target_ip} ---")

    info = _wait_for_serial(serial, timeout_s=10.0)
    if info is None:
        print(f"  [SKIP] Serial {serial} not found on network.")
        return False

    current_ip = str(info.get("ip", "?"))
    print(f"  Found at {current_ip}")

    # If not already on the main subnet, force_ip to a temp LLA first
    # so arena_api opens it on the correct interface.
    temp_ip = TEMP_LLA[serial]
    if not current_ip.startswith("172.16.9.") and current_ip != temp_ip:
        print(f"  force_ip -> {temp_ip} (temp LLA, single thread) ...")
        forced = dict(info)
        forced["ip"]             = temp_ip
        forced["subnetmask"]     = LLA_SUBNET
        forced["defaultgateway"] = "0.0.0.0"
        system.force_ip(forced)
        print("  Waiting 6s for camera network stack ...")
        time.sleep(6.0)

        info = _wait_for_serial(serial, timeout_s=12.0)
        if info is None:
            print(f"  [FAIL] Did not reappear after force_ip to {temp_ip}.")
            return False
        print(f"  Now at {info.get('ip')}. Opening ...")
    else:
        print(f"  Opening directly ...")

    try:
        devices = system.create_device(device_infos=info)
        dev = devices[0]
        nm  = dev.nodemap

        # Set persistent IP
        nm["GevCurrentIPConfigurationPersistentIP"].value = True
        nm["GevCurrentIPConfigurationDHCP"].value         = False
        nm["GevPersistentIPAddress"].value                = target_ip
        nm["GevPersistentSubnetMask"].value               = SUBNET
        nm["GevPersistentDefaultGateway"].value           = GATEWAY

        # Disable ARP conflict detection — prevents false disconnects in
        # multi-camera setups when both cameras are on the same subnet.
        try:
            nm["GevCurrentIPConfigurationPersistentIPProbeARP"].value = False
        except Exception:
            pass  # not all firmware versions expose this node

        # Save to UserSet1 and make it default
        nm["UserSetSelector"].value = "UserSet1"
        nm["UserSetSave"].execute()
        nm["UserSetDefault"].value = "UserSet1"

        system.destroy_device(dev)
        print(f"  [OK] Persistent IP set: {target_ip}/24 on serial {serial} ({label}).")
        print(f"       Power-cycle this camera to apply.")
        return True

    except Exception as exc:
        print(f"  [FAIL] {exc}")
        return False


def main():
    print("Discovering cameras (up to 10s) ...")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if list(system.device_infos):
            break
        time.sleep(0.5)

    infos = list(system.device_infos)
    if not infos:
        print("No cameras found. Check cable and power.")
        return

    print(f"Found {len(infos)} camera(s):")
    for i in infos:
        print(f"  serial={i.get('serial')}  ip={i.get('ip')}  mac={i.get('mac')}")
    print()

    # Process cameras ONE AT A TIME — force_ip is not concurrent-safe.
    results = {}
    for serial, cfg in CAMERAS.items():
        ok = set_persistent_ip(serial, cfg["label"], cfg["target_ip"])
        results[serial] = ok
        print(f"Waiting 4s before next camera ...")
        time.sleep(4.0)
        print()

    print("=" * 50)
    all_ok = all(results.values())
    for serial, ok in results.items():
        cfg = CAMERAS[serial]
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {serial} ({cfg['label']}) -> {cfg['target_ip']}")

    if all_ok:
        print()
        print("SUCCESS. Power-cycle BOTH cameras now.")
        print("After reboot they will be at:")
        print("  overhead (253102168) -> 172.16.9.21")
        print("  eoa      (253102169) -> 172.16.9.22")
        print()
        print("Then update machine_config.yaml camera IPs and")
        print("set USE_FORCE_IP=False in the driver (or remove it).")
    else:
        print()
        print("Some cameras FAILED. Check output above.")


if __name__ == "__main__":
    main()
