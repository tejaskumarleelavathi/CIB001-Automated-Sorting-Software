# Calibration — Architecture & Implementation Plan

## What "Calibration" Means in This System

There are **two coordinate systems** that must be bridged:

- **Camera space** — pixels (0, 0) to (5320, 4600) on the TRI245S-CC sensor
- **Robot space** — millimetres (X, Y) in the Epson T3-B's world coordinate system

The camera sees bugs at pixel coordinates. The robot moves to mm coordinates. Calibration is the one-time process of computing the mathematical transform that converts any pixel location into the corresponding robot XY position.

There are **two separate calibration problems** in this system, one per camera.

---

## Calibration 1 — Overhead Camera → Robot XY (Homography)

### What it does
Maps pixel coordinates from the overhead camera image → robot world coordinates (mm).
This is how "bug at pixel (2400, 1800)" becomes "robot go to X=234.5, Y=118.2".

### Method: Homography (perspective transform)
A 3×3 matrix **H** is computed from a set of known point correspondences:
```
(pixel_x, pixel_y)  ↔  (robot_x_mm, robot_y_mm)
```

At runtime, for every detected specimen centroid:
```python
import cv2, numpy as np

pixel_pt = np.array([[[px, py]]], dtype=np.float32)
robot_pt = cv2.perspectiveTransform(pixel_pt, H)
rx, ry = float(robot_pt[0][0][0]), float(robot_pt[0][0][1])
```

### Why homography and not a simple scale factor?
A flat scale (pixels/mm) only works if:
- The camera is perfectly perpendicular to the dish surface (no tilt)
- There is zero lens distortion
- The optical axis passes exactly through the centre of the dish

In practice none of these are perfect. A homography handles mild tilt, offset, and perspective without needing to characterise the lens mathematically.

**If there is significant lens distortion** (barrel/pincushion), an undistort step should be applied to the image before the homography. For a machine vision lens at f/8–f/11 with a large working distance, distortion is usually < 1% and can be ignored for this application. If specimens are being picked incorrectly at the dish edges, distortion may be the cause — see "Lens Distortion (optional)" below.

### Minimum calibration points
- Minimum: **4 points** (required to solve H)
- Recommended: **9–16 points** arranged in a grid across the full dish area
- More points at the edges gives better accuracy at the edges (where distortion is highest)

### Calibration target options
Three options, in order of preference:

**Option A — Printed dot grid (preferred)**
- Print a grid of black dots on white paper (or white dots on black)
- Space: 20 mm apart (matches robot step size well)
- Size: each dot ~5 mm diameter (detectable but not too large)
- Laminate or place under glass to keep flat in the dish
- OpenCV `cv2.findContours` or `cv2.SimpleBlobDetector` finds dot centres automatically

**Option B — ArenaView + checkerboard**
- Use a standard chess-board calibration target
- ArenaView may have a calibration tool that can extract corner points
- Corner points exported as pixel coordinates → matched to robot coordinates manually
- See "ArenaView for Calibration" section below

**Option C — No printed target (use plate wells)**
- The plate wells themselves are the calibration points
- Robot visits each of 4–9 wells at known XY positions (from plate config file)
- Overhead image taken, wells detected as circular blobs in image
- Pixel centres of wells matched to robot XY from plate config
- Advantage: no extra target required, calibration is always specific to the actual plate fixture
- Disadvantage: only valid when a plate is seated; wells are small and may be hard to detect

### One-time calibration procedure (physical steps)
1. Place calibration target flat in the sorting dish area
2. On the HMI calibration screen (engineer role only), press **Start Calibration**
3. Software runs `calibration.run_overhead_calibration(robot, camera, lighting)`:
   a. Turn backlight ON
   b. Capture overhead image
   c. Detect calibration target points in image (OpenCV blob/contour detection)
   d. For each detected point, prompt operator to confirm (or auto-match if target is known)
   e. Record robot XY for each point — either from plate config (option C) or by having robot physically visit each point
   f. `cv2.findHomography(pixel_pts, robot_pts)` → H matrix
   g. Log reprojection error — reject if RMSE > 0.5 mm
4. Save H to `C:\LabAutomation\config\calibration_overhead.npy`
5. Display reprojection error and sample point check on screen

### When to recalibrate
- Camera is moved or re-mounted
- Robot is rehomed with a different origin
- Dish fixture is repositioned
- Pick accuracy degrades at dish edges

### Runtime usage
```python
H = np.load(r"C:\LabAutomation\config\calibration_overhead.npy")
# In pipeline.py, after finding pixel centroids:
robot_pts = [pixel_to_robot(px, py, H) for (px, py) in pixel_centroids]
```

---

## Calibration 2 — EoA Camera → Refined Pick Offset

### What it does
The EoA (end-of-arm) camera is mounted on the robot arm. When the robot moves to the overhead-computed position (rx, ry), the EoA camera looks down at approximately that point.

The EoA camera does **not** need to know absolute robot coordinates. It only needs to compute:
- **Is this a real specimen?** (go/no-go)
- **How far is the specimen centre from the image centre?** (refinement offset in mm)

### Camera-to-pipette offset (static, measured once)
The EoA camera lens centre is not perfectly aligned with the pipette tip. There is a fixed physical offset (Δx_offset, Δy_offset) in mm. This is measured once:
1. Place a dot on the dish
2. Robot moves pipette tip directly over dot (confirmed by eye or using RC+ teach mode)
3. Take EoA image — dot appears at some pixel (dot_px, dot_py)
4. Image centre = (width/2, height/2)
5. Offset = ((dot_px - width/2) / scale, (dot_py - height/2) / scale)
6. Save as CAMERA_TO_PIPETTE_OFFSET_X_MM and CAMERA_TO_PIPETTE_OFFSET_Y_MM in machine_config.yaml

### EoA pixel/mm scale (measured once)
At the fixed EoA working distance (robot in imaging position, camera at known height above dish):
1. Robot moves a known distance (e.g. 2.0 mm in X)
2. Observe how many pixels a reference point moves in EoA image
3. scale_px_per_mm = pixel_shift / 2.0
4. Save as EOA_SCALE_PX_PER_MM in machine_config.yaml

### Runtime refinement (per pick)
```python
image_cx, image_cy = frame.shape[1] / 2, frame.shape[0] / 2
specimen_px, specimen_py = detect_eoa_centroid(frame)

offset_px = (specimen_px - image_cx, specimen_py - image_cy)
offset_mm = (offset_px[0] / EOA_SCALE_PX_PER_MM,
             offset_px[1] / EOA_SCALE_PX_PER_MM)

pick_x = rx + offset_mm[0] + CAMERA_TO_PIPETTE_OFFSET_X_MM
pick_y = ry + offset_mm[1] + CAMERA_TO_PIPETTE_OFFSET_Y_MM
```

---

## Lens Distortion (optional, apply only if needed)

If pick accuracy is poor at dish edges, measure and apply lens distortion correction.

### How to measure
Use a checkerboard target and `cv2.calibrateCamera()`:
```python
# Standard OpenCV camera calibration
ret, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
    objpoints, imgpoints, image_size, None, None
)
# Save
np.save("camera_matrix.npy", camera_matrix)
np.save("dist_coeffs.npy", dist_coeffs)

# At runtime, before applying homography:
undistorted = cv2.undistort(frame, camera_matrix, dist_coeffs)
```

For a machine vision lens at f/8–f/11, distortion is typically < 1% and can be skipped unless pick errors at the dish periphery are > 1 mm.

---

## ArenaView for Calibration

ArenaView is Lucid's GUI application for camera configuration and image capture. It is **not** a calibration tool in the metrology sense — it does not compute homographies, robot transforms, or pixel/mm mappings. What it **can** help with:

- **Capture calibration images** — take the overhead image at fixed exposure settings for the dot grid
- **Verify focus and exposure** before running calibration — confirm image quality before computing H
- **Intrinsic calibration (if it has a checkerboard tool)** — some Lucid versions expose an OpenCV-based intrinsic calibration workflow for distortion coefficients (confirm in installed version)
- **Live stream** — view the dish while physically adjusting the calibration target

**Bottom line:** Use ArenaView to set up the camera correctly and capture images. The actual calibration math (homography, pixel/mm, offsets) is done in Python using OpenCV. ArenaView is a setup/configuration tool, not a calibration solver.

**arena_api in calibration.py:** The calibration routine calls `camera_driver.capture()` just like the normal pipeline does — no ArenaView involvement at runtime. ArenaView is only useful as a manual tool during setup.

---

## Files to Create

| File | Purpose |
|------|---------|
| `cib/vision/calibration.py` | Core calibration functions (run_overhead_calibration, load_homography, pixel_to_robot, save/load calibration) |
| `C:\LabAutomation\config\calibration_overhead.npy` | Saved 3×3 homography matrix H (created at runtime by calibration routine) |
| `C:\LabAutomation\config\calibration_eoa.yaml` | EoA calibration constants: scale_px_per_mm, camera_to_pipette_offset_x_mm, camera_to_pipette_offset_y_mm |

### `cib/vision/calibration.py` — planned interface

```python
def run_overhead_calibration(
    camera: CameraDriver,
    lighting: LightingDriver,
    calibration_points_robot_xy: list[tuple[float, float]],
    # ^ robot XY coords of each calibration point (from plate config or manual input)
) -> np.ndarray:
    """
    Capture overhead image, detect calibration target points,
    match to robot coords, compute and return homography H.
    Raises CalibrationError if reprojection RMSE > 0.5mm or fewer than 4 points found.
    """
    ...

def save_homography(H: np.ndarray, path: str) -> None: ...
def load_homography(path: str) -> np.ndarray: ...

def pixel_to_robot(px: float, py: float, H: np.ndarray) -> tuple[float, float]:
    """Apply overhead homography: pixel → robot XY (mm)."""
    ...

def compute_eoa_scale(
    robot: RobotDriver,
    camera_eoa: CameraDriver,
    step_mm: float = 2.0,
) -> float:
    """
    Move robot a known distance, measure pixel shift in EoA image.
    Returns scale in px/mm.
    """
    ...

def measure_camera_to_pipette_offset(
    robot: RobotDriver,
    camera_eoa: CameraDriver,
) -> tuple[float, float]:
    """
    With pipette over a reference dot, detect dot in EoA image.
    Returns (offset_x_mm, offset_y_mm).
    """
    ...
```

---

## Dependency on Hardware (what's needed before calibration can run)

| Requirement | Status |
|-------------|--------|
| Overhead camera at 172.16.9.21 | ✅ Confirmed working |
| Backlight (DCS-100E at 172.16.9.20) | ✅ Confirmed working |
| Robot at 172.16.9.15:5001 | ⚠️ Blocked — E-stop circuit wiring (David) |
| EoA camera (2nd Lucid camera) | ⚠️ Not yet connected/identified |

**Overhead homography calibration can be done manually first** (without robot moving to points) if you physically place a dot grid target in the dish and measure the robot XY of each dot using the teach pendant. Then only the software (capture + detect + compute H) needs to run — no robot motion commands needed during calibration.

---

## Questions to Resolve Before Coding Calibration Routine

1. **Calibration target** — printed dot grid (recommended) or use plate wells (option C)?
2. **EoA working distance** — how far is the EoA camera from the dish surface when robot is in imaging position?
3. **ArenaView version installed** — does the installed version have any intrinsic calibration tool? (Check Help → About in ArenaView on the HMI)
4. **Who does the physical calibration?** Tejas with teach pendant, or Weifeng? This determines how much automation vs. manual steps we need in the calibration screen.
