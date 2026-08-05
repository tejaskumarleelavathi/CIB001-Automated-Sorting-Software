"""
diagnose_tool_offset_sign.py — pin down a sign/axis error in the Tool 1 offset
================================================================================

Use AFTER test_hold_position_for_u_jog.py has driven the robot to a target
and left it there (no home). This script grabs a live EoA frame, you click on
where the reference point (checkerboard corner, dot, pin — whatever you used
during eoa_tool_offset.py) ACTUALLY appears, and it works out exactly what
correction (cx, cy) needs, using the real camera-axis calibration data from
eoa_pixel_scale.json (not a guess — those numbers were measured by moving the
robot ±2mm and watching how the image shifts, so they capture the camera's
true mounting rotation relative to robot X/Y).

MATH
  If the crosshair (image centre) is where the camera should be, and the
  reference point appears offset by (dpx, dpy) pixels, that offset converts
  to a world-frame error (ex, ey) in mm via the measured camera axes:
      (dpx, dpy) = M @ (ex, ey)      where M's columns are the measured
                                      pixel-shift-per-mm for robot +X / +Y
  That world error is what the CURRENT (cx, cy) got wrong, still rotated by
  whatever U the robot was at:
      (ex, ey) = R(u) @ (delta_cx, delta_cy)
  So:
      (delta_cx, delta_cy) = R(-u) @ (ex, ey)
      corrected cx = current cx + delta_cx
      corrected cy = current cy + delta_cy

  If corrected cy ends up close to -1 * current cy (or cx close to -1 *
  current cx), that confirms a simple sign flip rather than a bad
  measurement.

USAGE
  & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/diagnose_tool_offset_sign.py
"""

import ctypes
import json
import math
import os
import sys

import cv2
import numpy as np

EOA_SERIAL    = "253102169"
EXPOSURE_US   = 120000
DISPLAY_WIDTH = 1280

SCALE_JSON = os.path.join(os.path.dirname(__file__), "..",
                          "camera_calibration", "output", "eoa_pixel_scale.json")

CURRENT_CX = 17.1876
CURRENT_CY = -77.0233


def grab_eoa_frame() -> np.ndarray:
    from arena_api.system import system

    print(f"Looking for EoA camera (serial {EOA_SERIAL}) ...")
    import time
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if any(str(i.get("serial", "")) == EOA_SERIAL for i in list(system.device_infos)):
            break
        time.sleep(0.5)

    info = next((i for i in list(system.device_infos)
                 if str(i.get("serial", "")) == EOA_SERIAL), None)
    if info is None:
        print("ERROR: EoA camera not found.")
        sys.exit(1)

    dev = system.create_device(device_infos=info)[0]
    nm  = dev.nodemap
    try:
        nm["PixelFormat"].value  = "BGR8"
        nm["ExposureAuto"].value = "Off"
        nm["ExposureTime"].value = float(EXPOSURE_US)
        nm["GainAuto"].value     = "Continuous"
    except Exception:
        pass

    dev.start_stream(2)
    for _ in range(3):
        b = dev.get_buffer(timeout=5000)
        dev.requeue_buffer(b)
    b = dev.get_buffer(timeout=5000)
    bpp = b.bits_per_pixel // 8
    h, w = b.height, b.width
    raw = (ctypes.c_uint8 * (h * w * bpp)).from_address(ctypes.addressof(b.pbytes))
    frame = np.ctypeslib.as_array(raw).copy().reshape(h, w, bpp)
    dev.requeue_buffer(b)
    dev.stop_stream()
    system.destroy_device()
    return frame


def main() -> None:
    print()
    print("=" * 70)
    print("  Tool Offset Sign Diagnosis")
    print("=" * 70)

    if not os.path.exists(SCALE_JSON):
        print(f"  [ERROR] {SCALE_JSON} not found.")
        sys.exit(1)
    with open(SCALE_JSON) as f:
        scale = json.load(f)

    delta_mm = scale["delta_mm"]
    axis_x   = np.array(scale["camera_axis_x"], dtype=float) / delta_mm   # px per mm, robot +X
    axis_y   = np.array(scale["camera_axis_y"], dtype=float) / delta_mm   # px per mm, robot +Y
    M = np.column_stack([axis_x, axis_y])   # pixel_shift = M @ [ex_mm, ey_mm]

    print(f"  Loaded camera axes from {SCALE_JSON}")
    print(f"    robot +1mm X -> {axis_x} px")
    print(f"    robot +1mm Y -> {axis_y} px")
    print()

    print("  Robot should already be sitting at the target from")
    print("  test_hold_position_for_u_jog.py (not homed).")
    input("  Press ENTER to grab a frame from the EoA camera ...")

    frame = grab_eoa_frame()
    h, w = frame.shape[:2]
    scale_disp = DISPLAY_WIDTH / w
    disp_w, disp_h = int(w * scale_disp), int(h * scale_disp)
    display = cv2.resize(frame, (disp_w, disp_h)).copy()

    ccx, ccy = disp_w // 2, disp_h // 2
    cv2.drawMarker(display, (ccx, ccy), (0, 255, 0), cv2.MARKER_CROSS, 40, 2)
    cv2.putText(display, "Click where the reference point ACTUALLY appears",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 220, 255), 2)

    click = {}

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            click["x"], click["y"] = x, y

    win = "Click the reference point (crosshair = where it should be)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, disp_w, disp_h)
    cv2.setMouseCallback(win, on_click)

    while "x" not in click:
        cv2.imshow(win, display)
        key = cv2.waitKey(50) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            print("  Cancelled.")
            cv2.destroyAllWindows()
            return

    cv2.circle(display, (click["x"], click["y"]), 8, (0, 0, 255), -1)
    cv2.imshow(win, display)
    cv2.waitKey(500)
    cv2.destroyAllWindows()

    # Pixel offset in FULL-RES frame coordinates (undo display scale)
    dpx = (click["x"] - ccx) / scale_disp
    dpy = (click["y"] - ccy) / scale_disp
    print(f"\n  Pixel offset (full-res): ({dpx:.1f}, {dpy:.1f}) px")

    # Solve world-frame error (ex, ey) in mm
    ex, ey = np.linalg.solve(M, np.array([dpx, dpy]))
    print(f"  World-frame error (camera actual - target): ({ex:.3f}, {ey:.3f}) mm")
    print(f"  Magnitude: {math.hypot(ex, ey):.3f} mm")

    u_str = input("\n  Current U angle from teach pendant (deg): ").strip()
    try:
        u_deg = float(u_str)
    except ValueError:
        print("  Invalid U — aborting.")
        return
    u = math.radians(u_deg)

    # Un-rotate by U to get the error in the tool-local frame
    cos_u, sin_u = math.cos(u), math.sin(u)
    delta_cx =  cos_u * ex + sin_u * ey
    delta_cy = -sin_u * ex + cos_u * ey

    cx_true = CURRENT_CX + delta_cx
    cy_true = CURRENT_CY + delta_cy

    print()
    print("─" * 70)
    print(f"  Current Tool 1 offset : cx = {CURRENT_CX:.4f}   cy = {CURRENT_CY:.4f}")
    print(f"  Tool-local error      : delta_cx = {delta_cx:+.3f}   delta_cy = {delta_cy:+.3f}")
    print(f"  Suggested corrected   : cx = {cx_true:.4f}   cy = {cy_true:.4f}")
    print("─" * 70)

    # Sign-flip hints
    if abs(cx_true - (-CURRENT_CX)) < 0.3 * abs(CURRENT_CX):
        print("  -> Looks like CX just needs its SIGN FLIPPED.")
    if abs(cy_true - (-CURRENT_CY)) < 0.3 * abs(CURRENT_CY):
        print("  -> Looks like CY just needs its SIGN FLIPPED.")
    if abs(cx_true - CURRENT_CY) < 0.3 * abs(CURRENT_CY) or abs(cy_true - CURRENT_CX) < 0.3 * abs(CURRENT_CX):
        print("  -> Looks like CX and CY might be SWAPPED.")

    print()
    print(f"  New SPEL+ command to try:")
    print(f"    Tool 1, {cx_true:.4f}, {cy_true:.4f}, 0, 0, 0, 0")
    print()


if __name__ == "__main__":
    main()
