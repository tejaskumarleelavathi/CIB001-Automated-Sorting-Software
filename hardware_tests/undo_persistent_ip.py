"""
undo_persistent_ip.py — Reset Lucid cameras to factory IP configuration.

Undoes the persistent IP set by set_camera_persistent_ip.py.
After running this + power-cycling, cameras will boot with DHCP/LLA
(link-local 169.254.x.x) just like they did out of the box.

The driver will use force_ip at connect time to assign 172.16.9.21/22.

Usage:
    python hardware_tests/undo_persistent_ip.py
"""

import time
from arena_api.system import system  # type: ignore[import]

# Known camera IPs (persistent IPs that were set)
CAMERA_IPS = ["172.16.9.21", "172.16.9.22"]
CAMERAS = {
    "253102168": "overhead",
    "253102169": "eoa",
}


def _discover(timeout_s: float = 15.0):
    """Discover cameras using both unicast and broadcast."""
    for ip in CAMERA_IPS:
        system.add_unicast_discovery_device(ip)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            return infos
        time.sleep(0.5)
    return list(system.device_infos)


def reset_camera(info: dict) -> bool:
    serial = str(info.get("serial", "?"))
    label = CAMERAS.get(serial, "unknown")
    current_ip = str(info.get("ip", "?"))

    print(f"\n--- Resetting {serial} ({label}) at {current_ip} ---")

    try:
        devices = system.create_device(device_infos=info)
        dev = devices[0]
        nm = dev.nodemap

        # 1. Disable persistent IP
        print("  Disabling persistent IP...")
        nm["GevCurrentIPConfigurationPersistentIP"].value = False

        # 2. Re-enable DHCP
        print("  Enabling DHCP...")
        nm["GevCurrentIPConfigurationDHCP"].value = True

        # 3. Re-enable LLA (Link-Local Addressing) as fallback
        try:
            nm["GevCurrentIPConfigurationLLA"].value = True
            print("  Enabling LLA (link-local)...")
        except Exception:
            print("  (LLA node not available — OK, it's usually on by default)")

        # 4. Re-enable ARP conflict detection (was disabled by persistent IP script)
        try:
            nm["GevCurrentIPConfigurationPersistentIPProbeARP"].value = True
            print("  Re-enabled ARP probe...")
        except Exception:
            pass

        # 5. Load factory defaults
        print("  Loading factory defaults (UserSetDefault -> Default)...")
        try:
            nm["UserSetSelector"].value = "Default"
            nm["UserSetLoad"].execute()
            print("  Factory defaults loaded.")
        except Exception as e:
            print(f"  (UserSetLoad failed: {e} — manual settings above should suffice)")

        # 6. Set default boot UserSet back to Default (not UserSet1)
        try:
            nm["UserSetDefault"].value = "Default"
            print("  Boot default set to 'Default'.")
        except Exception as e:
            print(f"  (Could not set UserSetDefault: {e})")

        # 7. Save current state to UserSet1 (overwrite the persistent IP config)
        try:
            nm["UserSetSelector"].value = "UserSet1"
            nm["UserSetSave"].execute()
            print("  Saved clean config to UserSet1 (overwrites old persistent IP).")
        except Exception:
            pass

        system.destroy_device(dev)
        print(f"  [OK] {serial} ({label}) reset to factory IP config.")
        print(f"       Power-cycle this camera to apply.")
        return True

    except Exception as exc:
        print(f"  [FAIL] {exc}")
        try:
            system.destroy_device()
        except Exception:
            pass
        return False


def main():
    print("Discovering cameras (up to 15s) ...")
    infos = _discover()

    if not infos:
        print("No cameras found. Check cables and power.")
        return

    # Deduplicate (unicast + broadcast can return same camera twice)
    seen = set()
    unique = []
    for info in infos:
        sn = str(info.get("serial", ""))
        if sn not in seen:
            seen.add(sn)
            unique.append(info)

    print(f"Found {len(unique)} camera(s):")
    for info in unique:
        print(f"  serial={info.get('serial')}  ip={info.get('ip')}")

    results = {}
    for info in unique:
        serial = str(info.get("serial", ""))
        ok = reset_camera(info)
        results[serial] = ok
        time.sleep(2.0)

    print("\n" + "=" * 50)
    print("Results:")
    for serial, ok in results.items():
        label = CAMERAS.get(serial, "unknown")
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {serial} ({label})")

    if any(results.values()):
        print()
        print("POWER-CYCLE the cameras now (restart CIB system).")
        print("After reboot, cameras will use DHCP/LLA (169.254.x.x).")
        print("The driver will force_ip them to 172.16.9.21/22 at connect time.")

    # Clean up unicast registrations
    for ip in CAMERA_IPS:
        try:
            system.remove_unicast_discovery_device(ip)
        except Exception:
            pass


if __name__ == "__main__":
    main()
