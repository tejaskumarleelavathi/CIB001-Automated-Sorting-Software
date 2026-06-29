"""
test_camera_connect.py — Test camera connection WITHOUT Arena View.

This script connects to a Lucid TRI245S-CC camera using the same
workarounds as the production driver:
  - StreamAutoNegotiatePacketSize = False  (bypasses TestPacketSize)
  - GevSCPSPacketSize = 1400              (fits USB-Ethernet MTU)
  - GevSCPD = 5000                        (inter-packet delay)

Arena View does NOT apply these workarounds, which is why it fails
on ASIX USB-Ethernet adapters. This script proves the cameras work.

Usage:
    python hardware_tests/test_camera_connect.py
"""

import ctypes
import time
import sys

import cv2
import numpy as np

# ── Config ────────────────────────────────────────────────────────
CAMERAS = {
    "253102168": {
        "name": "overhead",
        "exposure_us": 3000,
        "gain_db": 0.0,
        "pixel_format": "Mono8",
    },
    "253102169": {
        "name": "eoa",
        "exposure_us": 35000,
        "gain_db": 0.0,
        "pixel_format": "BGR8",
    },
}

# No unicast needed — adapter has 169.254.0.50, same subnet as cameras (LLA)

START_STREAM_RETRIES = 15
START_STREAM_RETRY_DELAY = 2.0
BUFFER_TIMEOUT_MS = 60_000


def test_camera(serial: str, cfg: dict) -> bool:
    from arena_api.system import system

    name = cfg["name"]

    print(f"\n{'='*60}")
    print(f"  Testing {name} camera (serial={serial})")
    print(f"{'='*60}")

    # 1. Broadcast discovery (adapter has secondary 169.254.0.50 — same subnet as cameras)

    # 2. Discover cameras
    print("  Discovering cameras (up to 15s)...")
    deadline = time.monotonic() + 15.0
    infos = []
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        for info in infos:
            if str(info.get("serial", "")) == serial:
                break
        else:
            time.sleep(0.5)
            continue
        break

    if not infos:
        print(f"  [FAIL] No cameras found on network.")
        return False

    # Find target camera
    target_info = None
    for info in infos:
        if str(info.get("serial", "")) == serial:
            target_info = info
            break

    if target_info is None:
        found = [str(i.get("serial", "")) for i in infos]
        print(f"  [FAIL] Serial {serial} not found. Found: {found}")
        return False

    current_ip = str(target_info.get("ip", ""))
    print(f"  Found camera at IP {current_ip} (using as-is, no force_ip)")

    # 4. Open device (with retries)
    print("  Opening device...")
    device = None
    for attempt in range(6):
        try:
            devices = system.create_device(device_infos=target_info)
            device = devices[0]
            print(f"  Device opened (attempt {attempt + 1})")
            break
        except Exception as exc:
            err_str = str(exc)
            if attempt < 5 and ("TIMEOUT" in err_str or "INVALID_INDEX" in err_str or "-1017" in err_str):
                print(f"  Retry {attempt + 1}/6: {err_str[:60]}")
                time.sleep(2.0)
                infos = list(system.device_infos)
                for info in infos:
                    if str(info.get("serial", "")) == serial:
                        target_info = info
                        break
                continue
            print(f"  [FAIL] create_device: {exc}")
            return False

    if not device:
        print("  [FAIL] Could not open device after 6 attempts")
        return False

    # 5. Configure — THE KEY WORKAROUNDS
    print("  Configuring camera (bypassing TestPacketSize)...")
    nm = device.nodemap

    try:
        nm["PixelFormat"].value = cfg["pixel_format"]
    except Exception as e:
        print(f"  (pixel_format warning: {e})")

    nm["ExposureAuto"].value = "Off"
    nm["ExposureTime"].value = float(cfg["exposure_us"])
    nm["GainAuto"].value = "Off"
    nm["Gain"].value = float(cfg["gain_db"])

    # Small packets for USB-Ethernet
    try:
        nm["GevSCPSPacketSize"].value = 1400
    except Exception:
        pass
    # Inter-packet delay
    try:
        nm["GevSCPD"].value = 5000
    except Exception:
        pass
    # CRITICAL: disable auto-negotiate so TestPacketSize is skipped
    try:
        tl = device.tl_stream_nodemap
        tl["StreamAutoNegotiatePacketSize"].value = False
        print("  StreamAutoNegotiatePacketSize = False (TestPacketSize bypassed)")
    except Exception as e:
        print(f"  (auto-negotiate warning: {e})")

    # 6. Start stream (with retries — ASIX needs several attempts)
    print("  Starting stream (may take several retries on ASIX adapter)...")
    for attempt in range(START_STREAM_RETRIES):
        try:
            device.start_stream(2)
            print(f"  Stream started! (attempt {attempt + 1})")
            break
        except Exception as exc:
            err_str = str(exc)
            retryable = "TIMEOUT" in err_str or "BUSY" in err_str
            if retryable and attempt < START_STREAM_RETRIES - 1:
                print(f"  Stream retry {attempt + 1}/{START_STREAM_RETRIES}: {err_str[:60]}")
                try:
                    device.stop_stream()
                except Exception:
                    pass
                time.sleep(START_STREAM_RETRY_DELAY)
                continue
            print(f"  [FAIL] start_stream: {exc}")
            system.destroy_device(device)
            return False

    # 7. Capture a frame
    print("  Capturing frame...")
    try:
        # Drain warmup frames
        for _ in range(3):
            buf = device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
            device.requeue_buffer(buf)

        buf = device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
        bpp = int(buf.bits_per_pixel) // 8
        h, w = int(buf.height), int(buf.width)
        total = h * w * bpp
        raw = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
        frame = np.ctypeslib.as_array(raw).copy()
        frame = frame.reshape(h, w) if bpp == 1 else frame.reshape(h, w, bpp)
        device.requeue_buffer(buf)

        print(f"  [OK] Captured frame: {frame.shape}, dtype={frame.dtype}")
        print(f"       min={frame.min()}, max={frame.max()}, mean={frame.mean():.1f}")

        # Save image
        out_path = f"hardware_tests/{name}_test_capture.png"
        cv2.imwrite(out_path, frame)
        print(f"       Saved to {out_path}")

    except Exception as exc:
        print(f"  [FAIL] capture: {exc}")
        try:
            device.stop_stream()
        except Exception:
            pass
        system.destroy_device(device)
        return False

    # 8. Clean up
    print("  Cleaning up...")
    try:
        device.stop_stream()
    except Exception:
        pass
    system.destroy_device(device)
    time.sleep(1.0)

    print(f"  [OK] {name} camera test PASSED")
    return True


def main():
    # Test specific camera or all
    if len(sys.argv) > 1:
        serial = sys.argv[1]
        if serial not in CAMERAS:
            print(f"Unknown serial {serial}. Known: {list(CAMERAS.keys())}")
            return
        test_camera(serial, CAMERAS[serial])
    else:
        # Test all cameras
        results = {}
        for serial, cfg in CAMERAS.items():
            ok = test_camera(serial, cfg)
            results[cfg["name"]] = ok
            time.sleep(2.0)

        print(f"\n{'='*60}")
        print("Results:")
        for name, ok in results.items():
            status = "PASSED" if ok else "FAILED"
            print(f"  [{status}] {name}")


if __name__ == "__main__":
    main()
