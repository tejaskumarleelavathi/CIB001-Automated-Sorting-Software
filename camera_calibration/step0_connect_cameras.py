"""
STEP 0 — Verify both cameras are reachable and save test images.

Run this BEFORE any calibration steps to confirm:
  - Overhead camera (172.16.9.21) is connected and captures OK
  - End-of-Arm (EoA) camera is connected and captures OK

Run force_camera_ip.py first if the overhead camera is unreachable.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step0_connect_cameras.py

Output:
    camera_calibration/output/overhead_test.png      <- overhead camera test image
    camera_calibration/output/eoa_test.png           <- end-of-arm camera test image
    camera_calibration/output/camera_serials.json    <- serial numbers (needed for other steps)
"""

import os
import sys
import json
import time
import ctypes
import numpy as np

# Identify cameras by serial number — reliable, survives force_camera_ip re-ordering.
# Serial 253102168 = overhead (TRI245S-C, confirmed from hardware tests).
# Serial 253102169 = end-of-arm.
OVERHEAD_SERIAL = "253102168"
EOA_SERIAL      = "253102169"

OUT_DIR      = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)

SERIALS_PATH = os.path.join(OUT_DIR, "camera_serials.json")


def wait_for_cameras(timeout_s: float = 8.0) -> list:
    """Wait up to timeout_s for at least one camera to appear on the network."""
    from arena_api.system import system
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            return infos
        time.sleep(0.5)
    return list(system.device_infos)


def capture_one_frame(device, pixel_format: str = "BGR8",
                      exposure_us: float | None = None) -> np.ndarray:
    """
    Capture a single frame from an open device.
    If exposure_us is given, sets a fixed exposure (ExposureAuto=Off).
    Otherwise uses auto-exposure with a 5-frame warm-up drain.
    """
    nodemap = device.nodemap

    # Pixel format
    try:
        nodemap["PixelFormat"].value = pixel_format
    except Exception:
        current_pf = nodemap["PixelFormat"].value
        print(f"    Could not set {pixel_format}, using current: {current_pf}")

    # Exposure
    if exposure_us is not None:
        try:
            nodemap["ExposureAuto"].value = "Off"
            nodemap["ExposureTime"].value = float(exposure_us)
            print(f"    ExposureAuto=Off  ExposureTime={exposure_us} us")
        except Exception as e:
            print(f"    Could not set fixed exposure: {e}")
    else:
        try:
            nodemap["ExposureAuto"].value = "Continuous"
        except Exception:
            pass

    # Gain — always auto
    try:
        nodemap["GainAuto"].value = "Continuous"
    except Exception:
        pass

    device.start_stream(2)
    try:
        # Drain a few frames so exposure settles
        for _ in range(5):
            buf = device.get_buffer(timeout=3000)
            device.requeue_buffer(buf)

        buf = device.get_buffer(timeout=3000)
        # Use pbytes + addressof — works across arena_api versions
        bpp    = int(buf.bits_per_pixel) // 8
        height = int(buf.height)
        width  = int(buf.width)
        total  = height * width * bpp
        raw    = (ctypes.c_uint8 * total).from_address(
                     ctypes.addressof(buf.pbytes))
        flat   = np.ctypeslib.as_array(raw).copy()

        if bpp == 1:
            frame = flat.reshape(height, width)
        else:
            frame = flat.reshape(height, width, bpp)

        device.requeue_buffer(buf)
    finally:
        device.stop_stream()

    return frame


def test_camera(system, info: dict, label: str, out_filename: str,
                exposure_us: float | None = None) -> dict | None:
    """
    Open camera described by `info`, capture one frame, save to out_filename.
    Returns a summary dict with serial, model, image size.
    """
    import cv2
    from arena_api.system import system as _sys

    serial = str(info.get("serial", ""))
    model  = str(info.get("model", "unknown"))
    ip     = str(info.get("ip", "?"))

    print(f"\n  [{label}] model={model}  serial={serial}  ip={ip}")

    try:
        devices = _sys.create_device(device_infos=info)
        device  = devices[0]
    except Exception as e:
        print(f"    ERROR: could not open device — {e}")
        return None

    try:
        frame = capture_one_frame(device, pixel_format="BGR8", exposure_us=exposure_us)
        print(f"    Captured frame: {frame.shape[1]}x{frame.shape[0]}")

        # Draw label so you can tell them apart at a glance
        annotated = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        cv2.putText(annotated, f"{label} — serial {serial}",
                    (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 3)
        cv2.putText(annotated, f"ip={ip}  model={model}",
                    (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

        out_path = os.path.join(OUT_DIR, out_filename)
        cv2.imwrite(out_path, annotated)
        print(f"    Saved: {out_path}")

        return {
            "label":  label,
            "serial": serial,
            "model":  model,
            "ip":     ip,
            "image":  out_filename,
            "shape":  [frame.shape[0], frame.shape[1]],
        }
    except Exception as e:
        import traceback
        print(f"    ERROR during capture — {e}")
        traceback.print_exc()
        return None
    finally:
        _sys.destroy_device()


def main():
    print("=" * 60)
    print("CIB Camera Calibration — Step 0: Connect Both Cameras")
    print("=" * 60)

    try:
        from arena_api.system import system
    except ImportError:
        print("\nERROR: arena_api not installed.")
        print("Run:  pip install <path-to-arena_api-whl>")
        sys.exit(1)

    print("\nScanning for cameras on the network (up to 8 seconds)...")
    infos = wait_for_cameras(timeout_s=8.0)

    if not infos:
        print("\nERROR: No cameras found.")
        print("Check:")
        print("  - Cameras are powered (PoE switch is on)")
        print("  - You are on the 172.16.9.x subnet")
        print("  - Run force_camera_ip.py to set the overhead camera IP first")
        sys.exit(1)

    print(f"\nFound {len(infos)} camera(s).")
    for info in infos:
        print(f"  serial={info.get('serial')}  ip={info.get('ip')}  model={info.get('model')}")

    # Identify cameras by serial number — IPs can swap after force_camera_ip
    overhead_info = None
    eoa_info      = None

    for info in infos:
        serial = str(info.get("serial", ""))
        if serial == OVERHEAD_SERIAL:
            overhead_info = info
        elif serial == EOA_SERIAL:
            eoa_info = info

    if overhead_info is None:
        print(f"\nWARNING: Overhead camera (serial {OVERHEAD_SERIAL}) not found.")
        if len(infos) >= 1:
            overhead_info = infos[0]
            print(f"  Falling back to first camera: serial={overhead_info.get('serial')}")

    if eoa_info is None and len(infos) >= 2:
        for info in infos:
            if info is not overhead_info:
                eoa_info = info
                break

    results = {}

    # ── Test overhead camera ──────────────────────────────────────────────────
    print("\n--- Testing OVERHEAD camera ---")
    if overhead_info is not None:
        r = test_camera(system, overhead_info, "OVERHEAD", "overhead_test.png",
                        exposure_us=2000)
        if r:
            results["overhead"] = r
    else:
        print("  No overhead camera found — skipping.")

    # ── Test EoA camera (auto-exposure) ──────────────────────────────────────
    print("\n--- Testing END-OF-ARM (EoA) camera ---")
    if eoa_info is not None:
        r = test_camera(system, eoa_info, "EoA", "eoa_test.png",
                        exposure_us=120000)
        if r:
            results["eoa"] = r
    else:
        if len(infos) < 2:
            print("  Only 1 camera found — EoA camera not detected.")
            print("  If both cameras are the same unit, power cycle the EoA camera.")
        else:
            print("  Could not identify EoA camera.")

    # ── Save serial map ───────────────────────────────────────────────────────
    if results:
        with open(SERIALS_PATH, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved camera info: {SERIALS_PATH}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if "overhead" in results and "eoa" in results:
        print("BOTH cameras OK")
        print(f"  Overhead  serial={results['overhead']['serial']}"
              f"  {results['overhead']['shape'][1]}x{results['overhead']['shape'][0]}")
        print(f"  EoA       serial={results['eoa']['serial']}"
              f"  {results['eoa']['shape'][1]}x{results['eoa']['shape'][0]}")
        print("\nOpen the saved images and confirm they look correct:")
        print(f"  {os.path.join(OUT_DIR, 'overhead_test.png')}")
        print(f"  {os.path.join(OUT_DIR, 'eoa_test.png')}")
        print("\nNext: run step1_detect_corners.py")
    elif "overhead" in results:
        print("OVERHEAD camera OK — EoA camera NOT found")
        print("You can still run step1 (overhead only).")
        print("EoA calibration will be done separately once EoA is reachable.")
    else:
        print("NO cameras captured successfully. Check connections and re-run.")
    print("=" * 60)


if __name__ == "__main__":
    main()
