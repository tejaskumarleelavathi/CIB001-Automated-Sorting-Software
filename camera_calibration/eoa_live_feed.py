"""
EoA camera live feed with centre crosshair.

Run this WHILE jogging the robot during calibration.
The crosshair shows the exact optical centre of the EoA camera.
Jog until the crosshair is centred on the target corner, then press ENTER
in the step2 terminal window.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/eoa_live_feed.py

Controls:
    Q  ->  quit
"""

import ctypes
import sys
import time
import numpy as np
import cv2

EOA_SERIAL    = "253102169"
EXPOSURE_US   = 120000        # fixed exposure for EoA
DISPLAY_WIDTH = 1280          # scale down for display (camera is 24MP)


def main():
    try:
        from arena_api.system import system
    except ImportError:
        print("arena_api not found. Run with cib_env python.")
        sys.exit(1)

    # ── Find EoA camera ───────────────────────────────────────────────────────
    print("Looking for EoA camera (serial {})...".format(EOA_SERIAL))
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if any(str(i.get("serial", "")) == EOA_SERIAL for i in infos):
            break
        time.sleep(0.5)

    eoa_info = next(
        (i for i in list(system.device_infos)
         if str(i.get("serial", "")) == EOA_SERIAL), None
    )
    if eoa_info is None:
        print("ERROR: EoA camera not found. Run force_camera_ip.py first.")
        sys.exit(1)

    devices = system.create_device(device_infos=eoa_info)
    device  = devices[0]
    print("Connected: serial={}  ip={}".format(
        eoa_info.get("serial"), eoa_info.get("ip")))

    nodemap = device.nodemap
    try:
        nodemap["PixelFormat"].value  = "BGR8"
    except Exception:
        pass
    try:
        nodemap["ExposureAuto"].value = "Off"
        nodemap["ExposureTime"].value = float(EXPOSURE_US)
    except Exception:
        pass
    try:
        nodemap["GainAuto"].value = "Continuous"
    except Exception:
        pass

    device.start_stream(2)
    print("Streaming. Press Q in the image window to quit.\n")

    cv2.namedWindow("EoA Live Feed — align crosshair with corner", cv2.WINDOW_NORMAL)

    try:
        while True:
            buf = device.get_buffer(timeout=3000)

            # Decode frame
            bpp   = buf.bits_per_pixel // 8
            h     = buf.height
            w     = buf.width
            total = h * w * bpp
            raw   = (ctypes.c_uint8 * total).from_address(
                        ctypes.addressof(buf.pbytes))
            flat  = np.ctypeslib.as_array(raw).copy()
            if bpp == 1:
                frame = flat.reshape(h, w)
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            else:
                frame = flat.reshape(h, w, bpp)

            device.requeue_buffer(buf)

            # Scale down for display
            scale    = DISPLAY_WIDTH / w
            disp_w   = int(w * scale)
            disp_h   = int(h * scale)
            display  = cv2.resize(frame, (disp_w, disp_h))

            # Centre crosshair
            cx, cy = disp_w // 2, disp_h // 2
            size   = 30
            color  = (0, 255, 0)   # green
            thick  = 2

            # Horizontal line
            cv2.line(display, (cx - size, cy), (cx + size, cy), color, thick)
            # Vertical line
            cv2.line(display, (cx, cy - size), (cx, cy + size), color, thick)
            # Centre dot
            cv2.circle(display, (cx, cy), 4, color, -1)
            # Small circle around centre
            cv2.circle(display, (cx, cy), size // 2, color, 1)

            # Label
            cv2.putText(display, "Camera centre  |  Q to quit",
                        (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
            cv2.putText(display,
                        "Jog until crosshair is ON the target corner",
                        (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 255), 2)

            cv2.imshow("EoA Live Feed — align crosshair with corner", display)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break

    finally:
        device.stop_stream()
        system.destroy_device()
        cv2.destroyAllWindows()
        print("Done.")


if __name__ == "__main__":
    main()
