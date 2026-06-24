# capture.py -- grab one frame, save as capture_HHMMSS.png (color if camera supports it)
# Usage:
#   & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/capture.py

import ctypes
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import cv2
from arena_api.system import system

print("Looking for cameras...")
infos = list(system.device_infos)
if not infos:
    print("ERROR: No cameras found.")
    exit(1)

info = infos[0]
print(f"Found: {info.get('model')}  serial={info.get('serial')}  ip={info.get('ip')}")

dev = system.create_device(device_infos=info)[0]
try:
    nm = dev.nodemap

    # Try color first, fall back to Mono8
    color = False
    for fmt in ("BGR8", "RGB8Packed", "BayerRG8"):
        try:
            nm["PixelFormat"].value = fmt
            color = (fmt != "BayerRG8")   # BayerRG8 needs demosaic
            bayer = (fmt == "BayerRG8")
            print(f"PixelFormat: {fmt}")
            break
        except Exception:
            pass
    else:
        nm["PixelFormat"].value = "Mono8"
        color = False
        bayer = False
        print("PixelFormat: Mono8 (color not available)")

    # Let auto-exposure converge before capturing
    nm["ExposureAuto"].value = "Continuous"
    nm["GainAuto"].value     = "Continuous"

    dev.start_stream(2)

    # Discard several frames while auto-exposure settles
    for _ in range(10):
        buf = dev.get_buffer(timeout=5000)
        dev.requeue_buffer(buf)

    # Grab the settled frame
    buf = dev.get_buffer(timeout=5000)
    bpp = buf.bits_per_pixel // 8
    nb  = buf.width * buf.height * bpp
    raw = (ctypes.c_uint8 * nb).from_address(ctypes.addressof(buf.pbytes))
    arr = np.ctypeslib.as_array(raw).copy()

    if bpp == 1:
        frame = arr.reshape(buf.height, buf.width)
    else:
        frame = arr.reshape(buf.height, buf.width, bpp)

    if bayer:
        frame = cv2.cvtColor(frame, cv2.COLOR_BayerRG2BGR)

    dev.requeue_buffer(buf)
    dev.stop_stream()
finally:
    system.destroy_device(dev)

ts  = datetime.now().strftime("%H%M%S")
out = Path(__file__).parent / f"capture_{ts}.png"
cv2.imwrite(str(out), frame)

print(f"shape={frame.shape}  min={frame.min()}  max={frame.max()}  mean={frame.mean():.1f}")
print(f"Saved: {out}")
