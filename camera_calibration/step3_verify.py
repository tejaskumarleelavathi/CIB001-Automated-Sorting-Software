"""
STEP 3 — Verify calibration accuracy on the real dish.

Run this AFTER step2_compute_homography.py.
Click any point in the overhead image → see predicted robot XY.
Jog robot to that XY → check if pipette lands where you clicked.

Usage:
    & "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step3_verify.py

Controls (image window):
    Left-click  → shows predicted robot XY for that pixel
    Q           → quit
"""

import os
import sys
import numpy as np
import cv2

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
H_LOCAL = os.path.join(OUT_DIR, "calibration_overhead.npy")


def pixel_to_robot(px: float, py: float, H: np.ndarray) -> tuple[float, float]:
    pt = np.array([[[px, py]]], dtype=np.float32)
    result = cv2.perspectiveTransform(pt, H)
    return float(result[0][0][0]), float(result[0][0][1])


clicked_points = []

def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        H, img_display = param
        rx, ry = pixel_to_robot(x, y, H)
        clicked_points.append((x, y, rx, ry))

        # Draw dot and label on display image
        cv2.circle(img_display, (x, y), 8, (0, 255, 0), -1)
        label = f"X={rx:.1f} Y={ry:.1f}"
        cv2.putText(img_display, label, (x + 12, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow("Calibration Verify — click any point", img_display)
        print(f"  Clicked pixel ({x}, {y})  →  robot X={rx:.2f} mm,  Y={ry:.2f} mm")
        print(f"  Jog robot to X={rx:.2f}, Y={ry:.2f} and check if pipette is over that spot.")


def main():
    print("="*60)
    print("CIB Camera Calibration — Step 3: Verify")
    print("="*60)

    if not os.path.exists(H_LOCAL):
        print(f"ERROR: No homography found at {H_LOCAL}")
        print("Run step2_compute_homography.py first.")
        sys.exit(1)

    H = np.load(H_LOCAL)
    print(f"Loaded H matrix from: {H_LOCAL}")

    # Use the raw capture from Step 1
    raw_path = os.path.join(OUT_DIR, "raw_capture.png")
    if not os.path.exists(raw_path):
        print(f"ERROR: No captured image at {raw_path}")
        print("Run step1_detect_corners.py first.")
        sys.exit(1)

    img = cv2.imread(raw_path)
    h, w = img.shape[:2]

    # Scale down for display if image is very large
    MAX_DIM = 1200
    scale = min(MAX_DIM / w, MAX_DIM / h, 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
        # Adjust H for the scaled display — NO, easier to just convert click coords back
        print(f"  Image scaled to {int(w*scale)}x{int(h*scale)} for display.")
        print(f"  (original was {w}x{h})")

    img_display = img.copy()
    cv2.putText(img_display, "Click any point to get robot XY. Press Q to quit.",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

    cv2.namedWindow("Calibration Verify — click any point", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("Calibration Verify — click any point",
                         on_mouse, param=(H, img_display))
    cv2.imshow("Calibration Verify — click any point", img_display)

    print("\nImage window is open.")
    print("LEFT-CLICK any point to see the predicted robot XY.")
    print("Press Q in the image window to quit.\n")

    while True:
        key = cv2.waitKey(50) & 0xFF
        if key == ord("q") or key == 27:
            break

    cv2.destroyAllWindows()

    if clicked_points:
        print("\nSummary of clicked points:")
        print(f"  {'Pixel':20s}  {'Robot XY':25s}")
        for x, y, rx, ry in clicked_points:
            print(f"  ({x:4d}, {y:4d})           X={rx:8.2f} mm,  Y={ry:8.2f} mm")

    print("\nCalibration verification done.")
    print("If the robot lands accurately on your clicked spots, calibration is good.")


if __name__ == "__main__":
    main()
