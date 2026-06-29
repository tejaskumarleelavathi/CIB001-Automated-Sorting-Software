"""
Capture one overhead frame and save it for dish detection tuning.
Run with: cib_env python hardware_tests/capture_overhead.py
"""
import time
import cv2
import numpy as np
from arena_api.system import system

CAMERA_IP = "172.16.9.21"
SERIAL = "253102168"
OUTPUT = "hardware_tests/overhead_frame.png"

print("Discovering camera...")
system.add_unicast_discovery_device(CAMERA_IP)
for _ in range(30):
    infos = list(system.device_infos)
    for i in infos:
        if str(i.get("serial", "")) == SERIAL:
            break
    else:
        time.sleep(0.5)
        continue
    break

target = [i for i in infos if str(i.get("serial", "")) == SERIAL][0]
print(f"Found at {target.get('ip')}, creating device...")
devices = system.create_device(device_infos=target)
device = devices[0]

nm = device.nodemap
nm["PixelFormat"].value = "Mono8"
nm["ExposureAuto"].value = "Off"
nm["ExposureTime"].value = 3000.0
nm["GainAuto"].value = "Off"
nm["Gain"].value = 0.0
try:
    nm["GevSCPSPacketSize"].value = 1400
except Exception:
    pass
try:
    device.tl_stream_nodemap["StreamAutoNegotiatePacketSize"].value = False
except Exception:
    pass

print("Starting stream...")
for attempt in range(8):
    try:
        device.start_stream(2)
        print("Stream started!")
        break
    except Exception as e:
        print(f"  Retry {attempt+1}: {str(e)[:60]}")
        try:
            device.stop_stream()
        except Exception:
            pass
        time.sleep(3.0)
else:
    print("FAILED to start stream after 8 attempts")
    system.destroy_device(device)
    exit(1)

# Drain warmup frames
import ctypes
for _ in range(10):
    buf = device.get_buffer(timeout=5000)
    device.requeue_buffer(buf)

# Capture keeper frame
buf = device.get_buffer(timeout=5000)
h, w = int(buf.height), int(buf.width)
bpp = int(buf.bits_per_pixel) // 8
total = h * w * bpp
raw = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
frame = np.ctypeslib.as_array(raw).copy().reshape(h, w)
device.requeue_buffer(buf)

print(f"Captured frame: {frame.shape}")
cv2.imwrite(OUTPUT, frame)
print(f"Saved to {OUTPUT}")

device.stop_stream()
system.destroy_device(device)
print("Done.")
