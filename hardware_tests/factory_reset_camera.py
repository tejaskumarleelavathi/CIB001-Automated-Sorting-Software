"""
factory_reset_camera.py — Full factory reset for Lucid TRI245S-CC cameras.

Resets ALL camera settings to factory defaults:
  - Loads factory default UserSet
  - Sets boot default to factory UserSet
  - Resets IP config to factory (DHCP=1, PersistentIP=0, LLA=1)
  - Clears persistent IP address
  - Overwrites UserSet1 with clean factory settings

IMPORTANT: Does NOT force_ip — connects to camera at its current
DHCP address (same 172.16.9.x subnet as host adapter).

Usage:
    python hardware_tests/factory_reset_camera.py              # reset all cameras
    python hardware_tests/factory_reset_camera.py 253102168    # reset overhead only
"""

import time
import sys

CAMERAS = {
    "253102168": "overhead",
    "253102169": "eoa",
}

# Known DHCP addresses and any other IPs cameras might be on
DISCOVERY_IPS = ["172.16.9.41", "172.16.9.35", "172.16.9.21", "172.16.9.22",
                 "169.254.1.168", "169.254.1.169"]


def discover_cameras(system, timeout_s=15.0):
    """Discover cameras using broadcast + unicast."""
    for ip in DISCOVERY_IPS:
        try:
            system.add_unicast_discovery_device(ip)
        except Exception:
            pass

    print(f"  Discovering cameras (up to {timeout_s}s)...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            return infos
        time.sleep(0.5)
    return list(system.device_infos)


def factory_reset(system, info) -> bool:
    serial = str(info.get("serial", "?"))
    label = CAMERAS.get(serial, "unknown")
    current_ip = str(info.get("ip", "?"))

    print(f"\n{'='*60}")
    print(f"  Factory resetting {serial} ({label}) at {current_ip}")
    print(f"{'='*60}")

    # Open device
    print("  Opening device...")
    device = None
    for attempt in range(6):
        try:
            devices = system.create_device(device_infos=info)
            device = devices[0]
            print(f"  Device opened (attempt {attempt + 1})")
            break
        except Exception as exc:
            err_str = str(exc)
            print(f"  Attempt {attempt + 1}/6 failed: {err_str[:80]}")
            if attempt < 5:
                time.sleep(2.0)
                # Re-discover in case IP changed
                infos = list(system.device_infos)
                for i in infos:
                    if str(i.get("serial", "")) == serial:
                        info = i
                        break
                continue
            print(f"  [FAIL] Could not open device: {exc}")
            return False

    if not device:
        print("  [FAIL] No device")
        return False

    nm = device.nodemap

    try:
        # ── Step 1: Load factory defaults ────────────────────────────
        print("\n  Step 1: Loading factory defaults...")
        try:
            nm["UserSetSelector"].value = "Default"
            nm["UserSetLoad"].execute()
            print("  [OK] Factory defaults loaded into active settings")
        except Exception as e:
            print(f"  [WARN] UserSetLoad failed: {e}")

        # ── Step 2: Set boot default to "Default" ────────────────────
        print("\n  Step 2: Setting boot default to 'Default'...")
        try:
            nm["UserSetDefault"].value = "Default"
            print("  [OK] Boot default = 'Default' (factory)")
        except Exception as e:
            print(f"  [WARN] UserSetDefault failed: {e}")

        # ── Step 3: Reset IP configuration ───────────────────────────
        print("\n  Step 3: Resetting IP configuration...")

        # Disable persistent IP
        try:
            nm["GevCurrentIPConfigurationPersistentIP"].value = False
            print("  [OK] PersistentIP = False")
        except Exception as e:
            print(f"  [WARN] PersistentIP disable failed: {e}")

        # Enable DHCP
        try:
            nm["GevCurrentIPConfigurationDHCP"].value = True
            print("  [OK] DHCP = True")
        except Exception as e:
            print(f"  [WARN] DHCP enable failed: {e}")

        # Enable LLA
        try:
            nm["GevCurrentIPConfigurationLLA"].value = True
            print("  [OK] LLA = True")
        except Exception as e:
            print(f"  [INFO] LLA node not writable (usually on by default): {e}")

        # Clear persistent IP address to 0.0.0.0
        try:
            nm["GevPersistentIPAddress"].value = 0
            print("  [OK] GevPersistentIPAddress = 0.0.0.0")
        except Exception as e:
            print(f"  [INFO] Could not clear persistent IP address: {e}")

        # Clear persistent subnet
        try:
            nm["GevPersistentSubnetMask"].value = 0
            print("  [OK] GevPersistentSubnetMask = 0.0.0.0")
        except Exception as e:
            print(f"  [INFO] Could not clear persistent subnet: {e}")

        # Clear persistent gateway
        try:
            nm["GevPersistentDefaultGateway"].value = 0
            print("  [OK] GevPersistentDefaultGateway = 0.0.0.0")
        except Exception as e:
            print(f"  [INFO] Could not clear persistent gateway: {e}")

        # Re-enable ARP probe
        try:
            nm["GevCurrentIPConfigurationPersistentIPProbeARP"].value = True
            print("  [OK] ARP probe re-enabled")
        except Exception as e:
            print(f"  [INFO] ARP probe node not available: {e}")

        # ── Step 4: Overwrite UserSet1 with clean settings ───────────
        print("\n  Step 4: Saving clean settings to UserSet1...")
        try:
            nm["UserSetSelector"].value = "UserSet1"
            nm["UserSetSave"].execute()
            print("  [OK] Clean config saved to UserSet1")
        except Exception as e:
            print(f"  [WARN] UserSet1 save failed: {e}")

        # ── Step 5: Read back and verify ─────────────────────────────
        print("\n  Step 5: Verifying settings...")
        try:
            dhcp = nm["GevCurrentIPConfigurationDHCP"].value
            persist = nm["GevCurrentIPConfigurationPersistentIP"].value
            boot_default = nm["UserSetDefault"].value
            print(f"  DHCP = {dhcp}")
            print(f"  PersistentIP = {persist}")
            print(f"  UserSetDefault = {boot_default}")
            if dhcp and not persist and boot_default == "Default":
                print("  [OK] All settings match factory defaults!")
            else:
                print("  [WARN] Some settings may not have applied")
        except Exception as e:
            print(f"  [WARN] Verification read failed: {e}")

    finally:
        # Clean up
        print("\n  Closing device...")
        try:
            system.destroy_device(device)
        except Exception:
            pass

    print(f"\n  [OK] {serial} ({label}) factory reset COMPLETE")
    print(f"  >>> POWER-CYCLE the camera now (unplug PoE / restart switch) <<<")
    return True


def main():
    from arena_api.system import system

    target_serial = sys.argv[1] if len(sys.argv) > 1 else None

    infos = discover_cameras(system)
    if not infos:
        print("No cameras found. Check cables and power.")
        return

    # Deduplicate
    seen = set()
    unique = []
    for info in infos:
        sn = str(info.get("serial", ""))
        if sn not in seen:
            seen.add(sn)
            unique.append(info)

    print(f"Found {len(unique)} camera(s):")
    for info in unique:
        sn = str(info.get("serial", ""))
        label = CAMERAS.get(sn, "unknown")
        print(f"  serial={sn} ({label})  ip={info.get('ip')}")

    results = {}
    for info in unique:
        sn = str(info.get("serial", ""))
        if target_serial and sn != target_serial:
            continue
        ok = factory_reset(system, info)
        results[sn] = ok
        time.sleep(2.0)

    if not results:
        if target_serial:
            print(f"\nSerial {target_serial} not found on network.")
        return

    print(f"\n{'='*60}")
    print("Results:")
    for sn, ok in results.items():
        label = CAMERAS.get(sn, "unknown")
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {sn} ({label})")

    if any(results.values()):
        print("\n  IMPORTANT: Power-cycle the camera(s) now!")
        print("  After reboot, they will use DHCP/LLA (factory defaults).")

    # Clean up unicast
    for ip in DISCOVERY_IPS:
        try:
            system.remove_unicast_discovery_device(ip)
        except Exception:
            pass


if __name__ == "__main__":
    main()
