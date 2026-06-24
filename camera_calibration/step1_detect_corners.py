"""
STEP 1 — Capture overhead image and detect checkerboard corners.

Run this at the lab with the calibration target in the sorting dish.
No robot needed. No backlight — use normal room lighting.

Usage:
    & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" camera_calibration/step1_detect_corners.py

Output (saved next to this script):
    camera_calibration/output/raw_capture.png          <- what the camera saw
    camera_calibration/output/corners_detected.png     <- same image with corners drawn
    camera_calibration/output/corners_numbered.png     <- key corners labelled (use as robot jog guide)
    camera_calibration/output/corners.json             <- pixel coordinates of all 30 corners
"""

import sys
import os
import json
import cv2
import numpy as np

# ── Checkerboard spec — must match the printed target ────────────────────────
INNER_COLS = 6   # inner corners across  (7 squares → 6 corners)
INNER_ROWS = 5   # inner corners down    (6 squares → 5 corners)
SQUARE_MM  = 20  # physical square size in mm
# ─────────────────────────────────────────────────────────────────────────────

# Overhead camera — identified by serial number (immune to IP re-ordering by force_camera_ip)
OVERHEAD_SERIAL = "253102168"
# Step 1 uses AUTO exposure — room lights on, NO backlight.
# (2000us is for backlight/bug-sorting mode, far too dark for room light only.)
OVERHEAD_EXPOSURE_US = None   # None = auto

OUT_DIR    = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)


def capture_from_camera() -> np.ndarray:
    """Connect to overhead camera by serial and grab one frame."""
    import ctypes
    try:
        from arena_api.system import system
        import time

        # Wait up to 8s for cameras to appear
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            infos = list(system.device_infos)
            if infos:
                break
            time.sleep(0.5)

        if not infos:
            raise RuntimeError("No cameras found. Run force_camera_ip.py first.")

        # Find overhead by serial
        overhead_info = next(
            (i for i in infos if str(i.get("serial", "")) == OVERHEAD_SERIAL), None
        )
        if overhead_info is None:
            serials = [i.get("serial") for i in infos]
            raise RuntimeError(
                f"Overhead camera (serial {OVERHEAD_SERIAL}) not found. "
                f"Found serials: {serials}. Run force_camera_ip.py first."
            )

        devices = system.create_device(device_infos=overhead_info)
        device  = devices[0]
        ip      = overhead_info.get("ip", "?")
        print(f"  Found overhead: serial={OVERHEAD_SERIAL}  ip={ip}")

        nodemap = device.nodemap
        nodemap["PixelFormat"].value  = "BGR8"
        if OVERHEAD_EXPOSURE_US is None:
            nodemap["ExposureAuto"].value = "Continuous"
            print(f"  ExposureAuto=Continuous  GainAuto=Continuous")
        else:
            nodemap["ExposureAuto"].value = "Off"
            nodemap["ExposureTime"].value = float(OVERHEAD_EXPOSURE_US)
            print(f"  ExposureTime={OVERHEAD_EXPOSURE_US} us  GainAuto=Continuous")
        nodemap["GainAuto"].value     = "Continuous"

        device.start_stream(2)
        # Drain frames so auto-exposure/gain settle (10 frames)
        print("  Waiting for auto-exposure to settle...")
        for _ in range(10):
            buf = device.get_buffer(timeout=5000)
            device.requeue_buffer(buf)

        buf   = device.get_buffer(timeout=5000)
        bpp   = buf.bits_per_pixel // 8
        h     = buf.height
        w     = buf.width
        total = h * w * bpp
        raw   = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
        frame = np.ctypeslib.as_array(raw).copy().reshape(h, w, bpp)
        device.requeue_buffer(buf)

        device.stop_stream()
        system.destroy_device()
        print(f"  Captured: {frame.shape[1]}x{frame.shape[0]} px")
        return frame

    except ImportError:
        print("arena_api not available.")
        print("Run with:")
        print("  C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe camera_calibration/step1_detect_corners.py")
        sys.exit(1)


def detect_corners(frame: np.ndarray):
    """
    Find checkerboard inner corners in the image.
    Runs on full resolution for maximum accuracy, then refines to sub-pixel.
    Returns corners or None if not found.
    """
    print(f"\nLooking for {INNER_COLS}x{INNER_ROWS} = {INNER_COLS*INNER_ROWS} inner corners...")
    print(f"  Image size: {frame.shape[1]}x{frame.shape[0]} px  (this may take a few minutes)")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    flags = (cv2.CALIB_CB_ADAPTIVE_THRESH +
             cv2.CALIB_CB_NORMALIZE_IMAGE +
             cv2.CALIB_CB_FAST_CHECK)

    found, corners = cv2.findChessboardCorners(gray, (INNER_COLS, INNER_ROWS), flags)

    if not found:
        print("\n  FAILED — corners not detected.")
        print("  Check:")
        print("    - Is the target completely flat? (no curling edges)")
        print("    - Is the whole board visible in the image?")
        print("    - Is there enough light? Try turning on the room lights.")
        print("    - Is the image in focus?")
        print(f"  Raw image saved to: {os.path.join(OUT_DIR, 'raw_capture.png')}")
        print("  Open it and check visually.")
        return None

    # Refine to sub-pixel accuracy
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)

    print(f"  SUCCESS — found all {len(corners)} corners.")
    return corners


def save_outputs(frame: np.ndarray, corners) -> None:
    """Save raw image, annotated image, numbered image, and JSON."""
    h, w = frame.shape[:2]

    # 1. Raw capture
    raw_path = os.path.join(OUT_DIR, "raw_capture.png")
    cv2.imwrite(raw_path, frame)
    print(f"\nSaved: {raw_path}")

    # 2. Corners drawn on image
    annotated = frame.copy()
    cv2.drawChessboardCorners(annotated, (INNER_COLS, INNER_ROWS), corners, True)
    ann_path = os.path.join(OUT_DIR, "corners_detected.png")
    cv2.imwrite(ann_path, annotated)
    print(f"Saved: {ann_path}")

    # 3. Numbered image — shows which number = which corner (for robot jog guide)
    numbered = frame.copy()
    # Draw all corners as small circles
    for i, pt in enumerate(corners):
        x, y = int(pt[0][0]), int(pt[0][1])
        cv2.circle(numbered, (x, y), 12, (0, 255, 0), 2)

    # Label the 4 outer corners + centre with large numbers (the ones user will jog to)
    key_indices = {
        0:                        "C0\n(TOP-LEFT)",
        INNER_COLS - 1:           "C1\n(TOP-RIGHT)",
        INNER_COLS * INNER_ROWS // 2: "C2\n(CENTRE)",
        INNER_COLS * (INNER_ROWS - 1): "C3\n(BOT-LEFT)",
        INNER_COLS * INNER_ROWS - 1:  "C4\n(BOT-RIGHT)",
    }

    for idx, label in key_indices.items():
        if idx >= len(corners):
            continue
        x, y = int(corners[idx][0][0]), int(corners[idx][0][1])
        cv2.circle(numbered, (x, y), 20, (0, 0, 255), 3)
        # Put label above the point
        for i, line in enumerate(label.split("\n")):
            cv2.putText(numbered, line, (x + 15, y - 20 + i * 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)

    num_path = os.path.join(OUT_DIR, "corners_numbered.png")
    cv2.imwrite(num_path, numbered)
    print(f"Saved: {num_path}")

    # 4. JSON — pixel coordinates of all corners
    corner_data = {
        "inner_cols": INNER_COLS,
        "inner_rows": INNER_ROWS,
        "square_mm":  SQUARE_MM,
        "total_corners": len(corners),
        "key_corners": {
            "C0_top_left":    0,
            "C1_top_right":   INNER_COLS - 1,
            "C3_bot_left":    INNER_COLS * (INNER_ROWS - 1),
            "C4_bot_right":   INNER_COLS * INNER_ROWS - 1,
        },
        "corners_px": [
            {"index": i, "x_px": float(pt[0][0]), "y_px": float(pt[0][1])}
            for i, pt in enumerate(corners)
        ]
    }
    json_path = os.path.join(OUT_DIR, "corners.json")
    with open(json_path, "w") as f:
        json.dump(corner_data, f, indent=2)
    print(f"Saved: {json_path}")


def print_next_steps(corners) -> None:
    key = {
        "C0 (top-left)":    0,
        "C1 (top-right)":   INNER_COLS - 1,
        "C3 (bot-left)":    INNER_COLS * (INNER_ROWS - 1),
        "C4 (bot-right)":   INNER_COLS * INNER_ROWS - 1,
    }
    print("\n" + "="*60)
    print("STEP 1 COMPLETE")
    print("="*60)
    print("\nOpen  camera_calibration/output/corners_numbered.png")
    print("You will see the board with RED circles on 5 key corners.")
    print("\nNext (needs robot working):")
    print("  Jog robot teach pendant so pipette tip touches each")
    print("  RED corner, then read the X, Y from the pendant display.")
    print("\nCorners to visit (in this order):")
    for name, idx in key.items():
        x = corners[idx][0][0]
        y = corners[idx][0][1]
        print(f"  {name:20s}  pixel ({x:.1f}, {y:.1f})")
    print("\nThen run:  step2_compute_homography.py")
    print("="*60)


def main():
    print("="*60)
    print("CIB Camera Calibration — Step 1: Detect Corners")
    print("="*60)
    print("\nMake sure:")
    print("  - Calibration target is flat in the sorting dish")
    print("  - Room lights are ON (no backlight needed)")
    print("  - Camera IP forced: run force_camera_ip.py first\n")

    frame = capture_from_camera()

    raw_path = os.path.join(OUT_DIR, "raw_capture.png")
    cv2.imwrite(raw_path, frame)

    corners = detect_corners(frame)
    if corners is None:
        sys.exit(1)

    save_outputs(frame, corners)
    print_next_steps(corners)


if __name__ == "__main__":
    main()
