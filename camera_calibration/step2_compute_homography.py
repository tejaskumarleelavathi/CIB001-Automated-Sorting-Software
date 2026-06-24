"""
STEP 2 — Enter robot XY coordinates and compute the homography.

Run this AFTER step1_detect_corners.py.
Needs: corners.json from Step 1 + robot XY values read from teach pendant.

Usage:
    & "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step2_compute_homography.py

What you need before running:
    - corners_numbered.png open on screen (from Step 1 output)
    - Robot teach pendant — jog to each RED corner, read X and Y values
    - Type those X, Y values in when prompted

Output:
    camera_calibration\output\calibration_overhead.npy   <- the homography matrix H
    camera_calibration\output\reprojection_check.png     <- visual accuracy check
    C:\LabAutomation\config\calibration_overhead.npy     <- also copied here (runtime location)
"""

import json
import os
import shutil
import numpy as np
import cv2

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_DIR      = os.path.join(os.path.dirname(__file__), "output")
CORNERS_JSON = os.path.join(OUT_DIR, "corners.json")
H_LOCAL      = os.path.join(OUT_DIR, "calibration_overhead.npy")
H_RUNTIME    = r"C:\LabAutomation\config\calibration_overhead.npy"
# ─────────────────────────────────────────────────────────────────────────────

# Which corners we ask the user to jog to (index into corners array)
# Using 4 outer corners — well spread across the board for best accuracy
KEY_CORNERS = {
    "C0 — TOP-LEFT corner":     0,
    "C1 — TOP-RIGHT corner":    None,   # filled in after loading json
    "C3 — BOTTOM-LEFT corner":  None,
    "C4 — BOTTOM-RIGHT corner": None,
}


def load_corners() -> tuple[dict, list]:
    if not os.path.exists(CORNERS_JSON):
        raise FileNotFoundError(
            f"corners.json not found at {CORNERS_JSON}\n"
            "Run step1_detect_corners.py first."
        )
    with open(CORNERS_JSON) as f:
        data = json.load(f)
    corners_px = [(c["x_px"], c["y_px"]) for c in data["corners_px"]]
    return data, corners_px


def ask_robot_xy(label: str, corner_px: tuple) -> tuple[float, float]:
    """Prompt user for robot XY for one corner."""
    print(f"\n  Corner: {label}")
    print(f"  Pixel location in image: ({corner_px[0]:.1f}, {corner_px[1]:.1f})")
    print(f"  Open corners_numbered.png and find this corner.")
    print(f"  Jog robot pipette tip directly over it, read X and Y from pendant.")

    while True:
        try:
            rx = float(input(f"    Robot X (mm): "))
            ry = float(input(f"    Robot Y (mm): "))
            print(f"    Recorded: X={rx}, Y={ry}")
            confirm = input(f"    Correct? (y/n): ").strip().lower()
            if confirm == "y":
                return rx, ry
        except ValueError:
            print("    Enter a number (e.g. 234.5)")


def compute_homography(pixel_pts: list, robot_pts: list) -> np.ndarray:
    """Compute homography from pixel → robot XY pairs."""
    px = np.array(pixel_pts, dtype=np.float32)
    rb = np.array(robot_pts,  dtype=np.float32)
    H, mask = cv2.findHomography(px, rb, cv2.RANSAC, 5.0)
    inliers = int(mask.sum()) if mask is not None else len(px)
    print(f"\n  Homography computed. Inliers: {inliers}/{len(px)}")
    return H


def check_reprojection(H: np.ndarray, pixel_pts: list, robot_pts: list) -> float:
    """Show how accurate the homography is on the points we used."""
    print("\n  Reprojection check (should be < 0.5 mm error):")
    errors = []
    for (px, py), (rx_true, ry_true) in zip(pixel_pts, robot_pts):
        pt = np.array([[[px, py]]], dtype=np.float32)
        pred = cv2.perspectiveTransform(pt, H)[0][0]
        err = np.sqrt((pred[0] - rx_true)**2 + (pred[1] - ry_true)**2)
        errors.append(err)
        print(f"    pixel ({px:.0f},{py:.0f}) → predicted ({pred[0]:.2f},{pred[1]:.2f}) "
              f"vs actual ({rx_true:.2f},{ry_true:.2f})  error={err:.3f} mm")

    rmse = float(np.sqrt(np.mean(np.array(errors)**2)))
    print(f"\n  RMSE: {rmse:.3f} mm", end="  ")
    if rmse < 0.5:
        print("✓ GOOD")
    elif rmse < 1.0:
        print("⚠ Acceptable but consider adding more anchor points")
    else:
        print("✗ TOO HIGH — re-run and re-enter robot XY more carefully")
    return rmse


def save_reprojection_image(H: np.ndarray, pixel_pts: list, robot_pts: list) -> None:
    """Draw predicted vs actual on the captured image."""
    raw_path = os.path.join(OUT_DIR, "raw_capture.png")
    if not os.path.exists(raw_path):
        return
    img = cv2.imread(raw_path)
    for (px, py), (rx_true, ry_true) in zip(pixel_pts, robot_pts):
        pt = np.array([[[px, py]]], dtype=np.float32)
        pred = cv2.perspectiveTransform(pt, H)[0][0]
        err = np.sqrt((pred[0] - rx_true)**2 + (pred[1] - ry_true)**2)
        x, y = int(px), int(py)
        colour = (0, 255, 0) if err < 0.5 else (0, 165, 255) if err < 1.0 else (0, 0, 255)
        cv2.circle(img, (x, y), 15, colour, 3)
        cv2.putText(img, f"{err:.2f}mm", (x + 18, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2)
    out_path = os.path.join(OUT_DIR, "reprojection_check.png")
    cv2.imwrite(out_path, img)
    print(f"  Saved reprojection image: {out_path}")


def main():
    print("="*60)
    print("CIB Camera Calibration — Step 2: Compute Homography")
    print("="*60)
    print("\nBefore continuing, open this image on your screen:")
    print(f"  {os.path.join(OUT_DIR, 'corners_numbered.png')}")
    print("\nYou will jog the robot to each RED corner shown.")
    input("\nPress ENTER when ready...")

    # Load corners from Step 1
    data, corners_px = load_corners()
    inner_cols = data["inner_cols"]
    inner_rows = data["inner_rows"]

    # Determine key corner indices
    key = {
        "C0 — TOP-LEFT corner":     0,
        "C1 — TOP-RIGHT corner":    inner_cols - 1,
        "C3 — BOTTOM-LEFT corner":  inner_cols * (inner_rows - 1),
        "C4 — BOTTOM-RIGHT corner": inner_cols * inner_rows - 1,
    }

    print(f"\nLoaded {len(corners_px)} corner pixel positions from Step 1.")
    print("\nYou will enter robot XY for 4 corners.")
    print("Jog the robot teach pendant — move SLOWLY near each corner.")
    print("Lower the pipette gently until it just touches the corner point.")
    print("Read X and Y from the pendant display.\n")

    pixel_pts = []
    robot_pts = []

    for label, idx in key.items():
        px_pt = corners_px[idx]
        rx, ry = ask_robot_xy(label, px_pt)
        pixel_pts.append(px_pt)
        robot_pts.append((rx, ry))

    # Compute
    print("\nComputing homography...")
    H = compute_homography(pixel_pts, robot_pts)

    # Check accuracy
    rmse = check_reprojection(H, pixel_pts, robot_pts)

    # Save
    np.save(H_LOCAL, H)
    print(f"\nSaved H matrix: {H_LOCAL}")

    # Also copy to runtime location
    runtime_dir = os.path.dirname(H_RUNTIME)
    os.makedirs(runtime_dir, exist_ok=True)
    shutil.copy(H_LOCAL, H_RUNTIME)
    print(f"Copied to runtime location: {H_RUNTIME}")

    save_reprojection_image(H, pixel_pts, robot_pts)

    print("\n" + "="*60)
    if rmse < 1.0:
        print("CALIBRATION COMPLETE")
        print(f"RMSE = {rmse:.3f} mm")
        print("\nNext: run step3_verify.py to spot-check on the real dish.")
    else:
        print("CALIBRATION NEEDS IMPROVEMENT")
        print(f"RMSE = {rmse:.3f} mm — re-run and enter robot XY more carefully.")
    print("="*60)


if __name__ == "__main__":
    main()
