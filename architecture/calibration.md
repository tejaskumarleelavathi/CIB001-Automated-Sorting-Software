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

---

## Multi-Quadrant Arm Configuration & U-Angle Calibration

### Background

The Epson T3-B SCARA arm cannot reach all dish coordinates from a single arm configuration.
David confirmed the dish is divided into 4 quadrants, each requiring a different J2 arm pose and U (wrist) angle:

- **Top 2 quadrants**: U = 170.109° (current calibration)
- **Bottom 2 quadrants**: U = TBD (David to provide)

This works mechanically, but the EoA camera AND pipette are both mounted **off-center from the U rotation axis**. This means rotating U changes where the tool tip physically sits in world XY — calibration done at one U angle is not directly valid at another.

---

### Why U Rotation Matters When Tools Are Offset

When an offset tool (camera or pipette) is mounted at distance `r` from the U axis, rotating U by `ΔU` moves the tool tip in world XY:

```
tool_world_x = wrist_x + r × cos(U + α)
tool_world_y = wrist_y + r × sin(U + α)
```

Where `α` is the fixed mounting angle of the tool in the end-effector frame.

**Consequence**: the existing calibration at U=170.109° has the camera offset baked in implicitly. At a different U, the camera has physically moved — the same wrist XY no longer puts the camera over the same world point.

---

### Pipette Offset Rotates With U

If at U=170.109° the pipette appears to be `+30mm` in the world X direction from the camera centre, this is **not** a fixed X offset. It is the current direction of a fixed tool-frame vector. When U rotates:

```
Δangle = U_new − U_old   (radians)

pipette_offset_x_new = px × cos(Δangle) − py × sin(Δangle)
pipette_offset_y_new = px × sin(Δangle) + py × cos(Δangle)
```

Example — if U changes from 170.109° to 370° (Δ ≈ 200°):
- A pipette offset of `(+30mm, 0mm)` at U=170.109° becomes approximately `(−28mm, −10mm)` at U=370°
- The direction has completely changed — you cannot reuse the same (Δx, Δy) constants

---

### Measurement Procedure (one-time, per U angle)

You do NOT need to redo the full calibration grid for each quadrant. You need one reference measurement per U angle:

**Step 1 — Measure camera U-correction offset**
1. Place a small target point on the dish
2. At U=170.109°, command MOVE_IMAGE until camera crosshair is centered over the target. Record wrist position (x1, y1)
3. Change U to the bottom-quadrant angle. Command MOVE_IMAGE to the same (x1, y1)
4. Capture EoA image — target will have shifted in frame. Measure pixel shift → convert to mm using EoA scale
5. This Δx, Δy is the world-frame correction to apply to every MOVE_IMAGE command for the bottom quadrant

**Step 2 — Measure pipette-to-camera offset at each U**
At each U angle, the pipette offset in world frame is different (see formula above).
Options:
- **Option A (measure directly)**: At each U, position the robot so the pipette tip is over a target, take EoA image, measure where target appears relative to image centre
- **Option B (compute from geometry)**: Measure the physical offset `r` and angle `α` of the pipette in the end-effector frame (calipers or from CAD). Then compute the world-frame offset for any U analytically using the rotation formula above

---

### What Needs to Be Implemented

| # | Task | Where |
|---|------|--------|
| 1 | Decide quadrant boundaries (X/Y thresholds) | David to confirm |
| 2 | Get U angle for bottom 2 quadrants | David to provide |
| 3 | Add `quadrant_u_angles` and `quadrant_bounds` to `machine_config.yaml` | config |
| 4 | In `sort_server.prg` MOVE_IMAGE: accept U angle as third parameter `MOVE_IMAGE rx,ry,u` so Python can set per-quadrant U | `robot_programs/sort_server.prg` |
| 5 | In `EpsonT3BDriver.move_to_image_site()`: add `u_angle` parameter; pass it to MOVE_IMAGE | `cib/drivers/robot/driver.py` |
| 6 | In orchestrator: compute which quadrant a centroid falls in, look up correct U angle | `cib/core/orchestrator.py` |
| 7 | Apply camera U-correction offset (measured in Step 1 above) to the wrist XY before sending MOVE_IMAGE | `cib/core/orchestrator.py` |
| 8 | Measure camera U-correction offset for bottom quadrant U angle (physical, at lab) | lab work |
| 9 | Measure or compute pipette-to-camera offset at each U angle | lab work |
| 10 | Apply per-U pipette offset in pick command (when pick-place is implemented) | `cib/core/orchestrator.py` or `cib/drivers/robot/driver.py` |
| 11 | Store per-U offsets in `machine_config.yaml` or `calibration_eoa.yaml` | config |

---

### Config Shape (planned)

```yaml
# machine_config.yaml — quadrant arm configuration (values TBD from David)
robot:
  quadrants:
    - name: top_left
      u_angle_deg: 170.109
      camera_correction_x_mm: 0.0    # reference quadrant — no correction
      camera_correction_y_mm: 0.0
    - name: top_right
      u_angle_deg: 170.109
      camera_correction_x_mm: 0.0
      camera_correction_y_mm: 0.0
    - name: bottom_left
      u_angle_deg: ???               # David to provide
      camera_correction_x_mm: ???    # measure at lab (Step 1 above)
      camera_correction_y_mm: ???
    - name: bottom_right
      u_angle_deg: ???
      camera_correction_x_mm: ???
      camera_correction_y_mm: ???
  quadrant_split_x_mm: ???           # world X dividing left/right — David to confirm
  quadrant_split_y_mm: ???           # world Y dividing top/bottom — David to confirm
```

---

## Questions to Resolve Before Coding Calibration Routine

1. **Calibration target** — printed dot grid (recommended) or use plate wells (option C)?
2. **EoA working distance** — how far is the EoA camera from the dish surface when robot is in imaging position?
3. **ArenaView version installed** — does the installed version have any intrinsic calibration tool? (Check Help → About in ArenaView on the HMI)
4. **Who does the physical calibration?** Tejas with teach pendant, or Weifeng? This determines how much automation vs. manual steps we need in the calibration screen.
