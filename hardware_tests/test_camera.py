"""
test_camera.py — Lucid Vision camera discovery + capture test

Cameras: Lucid Vision Labs GigE Vision cameras (2x: overhead + end-of-arm)
SDK:     arena_api  (Lucid's first-party Python SDK)

BEFORE RUNNING — install arena_api:
    1. Download Arena SDK from: https://thinklucid.com/downloads-hub/
       (create a free account, download "Arena SDK for Windows")
    2. Install ArenaSDK_Windows_x64_*.exe
    3. The .whl is at:
       C:\\ProgramData\\LUCID Vision Labs\\Arena SDK\\ArenaPy\\
       (or similar path — check after installation)
    4. pip install arena_api-*.whl  (inside your cib_env)

NOTE: Cameras are on the PoE switch. If you can't reach them from your laptop
(different switch than main cell switch), run this script from the HMI computer
instead (once RDP is set up at 172.16.9.10).

Usage:
    conda run -n cib_env python test_camera.py            # discover all cameras
    conda run -n cib_env python test_camera.py overhead   # capture from overhead camera
    conda run -n cib_env python test_camera.py eoa        # capture from end-of-arm camera

Output images are saved as PNG in the current directory.
"""

import sys
import time
import ctypes
from pathlib import Path

import numpy as np

# ---- Check arena_api is installed -------------------------------------------
try:
    from arena_api.system import system  # type: ignore[import]
except ImportError:
    print("\n[ERROR] arena_api not installed.")
    print("Install steps:")
    print("  1. Download Arena SDK from https://thinklucid.com/downloads-hub/")
    print("  2. Install the .exe for Windows")
    print("  3. Find the .whl in C:\\ProgramData\\LUCID Vision Labs\\Arena SDK\\ArenaPy\\")
    print("  4. conda run -n cib_env pip install <path-to-whl>")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Camera serial numbers — fill these in once you've identified the cameras.
# Run with no arguments first to see the serial numbers printed for all cameras.
# Then label them physically (tape on the camera) and fill in below.
# ---------------------------------------------------------------------------
OVERHEAD_SERIAL = ""    # e.g. "241300001" — fill in after first discovery run
END_OF_ARM_SERIAL = ""  # e.g. "241300002" — fill in after first discovery run


def discover_cameras() -> None:
    """Print info for every camera found on the network."""
    print("\nScanning for Lucid cameras on the GigE network...")
    print("(Give it up to 5 seconds for GigE discovery...)\n")

    devices = _wait_for_cameras(timeout_s=5.0)

    if not devices:
        print("[ERROR] No cameras found.")
        print("Check:")
        print("  - Cameras are powered (PoE switch on)")
        print("  - Your machine is on the same subnet (172.16.9.x)")
        print("  - arena_api is installed correctly")
        return

    print(f"Found {len(devices)} camera(s):\n")
    for i, info in enumerate(devices):
        print(f"  Camera {i}:")
        for key, val in info.items():
            print(f"    {key}: {val}")
        print()

    print("=> Copy the 'serial' values above into OVERHEAD_SERIAL and")
    print("   END_OF_ARM_SERIAL at the top of this script, then label")
    print("   each camera physically so you know which is which.")


def _wait_for_cameras(timeout_s: float = 5.0) -> list:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = system.device_infos
        if infos:
            return list(infos)
        time.sleep(0.5)
    return list(system.device_infos)


def _open_camera_by_serial(serial: str):
    infos = system.device_infos
    for info in infos:
        if str(info.get("serial", "")) == serial:
            devices = system.create_device(device_infos=info)
            return devices[0]
    raise RuntimeError(
        f"Camera with serial '{serial}' not found.\n"
        f"Available cameras: {[d.get('serial') for d in infos]}\n"
        f"Run 'python test_camera.py' (no args) to discover cameras."
    )


def _open_first_camera():
    """Open whichever camera is found first — for discovery/testing."""
    infos = _wait_for_cameras(5.0)
    if not infos:
        raise RuntimeError("No cameras found on the network.")
    devices = system.create_device(device_infos=infos[0])
    return devices[0], infos[0]


def capture_frame(device) -> np.ndarray:
    """
    Capture a single frame from the camera and return it as a numpy uint8 array.
    Shape: (height, width) for Mono8, or (height, width, 3) for BGR8.
    """
    device.start_stream(2)   # OldestFirstOverwrite requires >= 2 buffers
    try:
        buffer = device.get_buffer(timeout=5000)   # ms
        bpp = buffer.bits_per_pixel // 8
        total_bytes = buffer.width * buffer.height * bpp

        # Build numpy array directly from the buffer's raw memory pointer
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


def run_capture(camera_name: str, serial: str) -> None:
    """Open a specific camera by serial, capture one frame, save to PNG."""
    print(f"\nOpening {camera_name} camera (serial: {serial}) ...")
    devices = _wait_for_cameras(5.0)
    device = _open_camera_by_serial(serial)

    try:
        # Print camera info
        nodemap = device.nodemap
        model = nodemap.nodes["DeviceModelName"].value
        dev_serial = nodemap.nodes["DeviceSerialNumber"].value
        print(f"  Model:  {model}")
        print(f"  Serial: {dev_serial}")

        # Set pixel format to Mono8 (grayscale) for simplicity
        # Comment this out if the camera only supports colour formats
        try:
            nodemap.nodes["PixelFormat"].value = "Mono8"
            print("  PixelFormat: Mono8")
        except Exception:
            pf = nodemap.nodes["PixelFormat"].value
            print(f"  PixelFormat: {pf} (could not set Mono8 — using default)")

        # Capture
        print("\nCapturing frame ...")
        frame = capture_frame(device)
        print(f"  Frame shape: {frame.shape}  dtype: {frame.dtype}")
        print(f"  Min pixel: {frame.min()}  Max pixel: {frame.max()}")

        # Save to PNG
        import cv2  # already in cib_env via opencv-python
        out_path = Path(f"capture_{camera_name}.png")
        cv2.imwrite(str(out_path), frame)
        print(f"\nSaved: {out_path.resolve()}")

    finally:
        system.destroy_device(device)
        print("Camera released.")


def run_first_camera_capture() -> None:
    """
    Capture from whichever camera is found first — useful when serials
    are not yet filled in but you want to test image capture.
    """
    print("\nSerials not configured — capturing from first camera found ...")
    devices_found = _wait_for_cameras(5.0)
    if not devices_found:
        print("[ERROR] No cameras found.")
        return

    device, info = _open_first_camera()
    serial = info.get("serial", "unknown")
    print(f"Using camera serial: {serial}")

    try:
        frame = capture_frame(device)
        print(f"Frame shape: {frame.shape}  dtype: {frame.dtype}")
        print(f"Min pixel: {frame.min()}  Max pixel: {frame.max()}")

        import cv2
        out_path = Path(f"capture_first_camera.png")
        cv2.imwrite(str(out_path), frame)
        print(f"\nSaved: {out_path.resolve()}")
        print(f"\nSerial for this camera: {serial}")
        print("Label the camera and add the serial to OVERHEAD_SERIAL or END_OF_ARM_SERIAL above.")

    finally:
        system.destroy_device(device)
        print("Camera released.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "discover"

    if arg == "discover":
        discover_cameras()

    elif arg == "overhead":
        if not OVERHEAD_SERIAL:
            print("[ERROR] OVERHEAD_SERIAL is not set.")
            print("Run 'python test_camera.py' first to discover serials.")
            print("Or run 'python test_camera.py first' to capture from whichever camera is found.")
            sys.exit(1)
        run_capture("overhead", OVERHEAD_SERIAL)

    elif arg == "eoa":
        if not END_OF_ARM_SERIAL:
            print("[ERROR] END_OF_ARM_SERIAL is not set.")
            print("Run 'python test_camera.py' first to discover serials.")
            sys.exit(1)
        run_capture("eoa", END_OF_ARM_SERIAL)

    elif arg == "first":
        # Capture from first camera found — no serial needed
        run_first_camera_capture()

    else:
        print(f"Unknown argument: {arg}")
        print("Usage: python test_camera.py [discover|overhead|eoa|first]")
        sys.exit(1)
