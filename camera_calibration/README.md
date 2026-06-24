# Camera Calibration Scripts

These scripts are run ONCE at the lab to calibrate the overhead camera.
They are separate from the main CIB software — run them manually, not as part of normal operation.

## What they produce
A file `C:\LabAutomation\config\calibration_overhead.npy` — the homography matrix H.
The main software loads this file at startup to convert bug pixel positions → robot XY coordinates.

## Run order

### Step 0 — Verify both cameras connect
```
& "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step0_connect_cameras.py
```
- Run this first to confirm overhead + EoA cameras are both reachable
- Run `force_camera_ip.py` before this if the overhead camera (172.16.9.21) is not found
- Saves `output\overhead_test.png`, `output\eoa_test.png`, `output\camera_serials.json`

### Step 1 — Detect corners (no robot needed)
```
& "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step1_detect_corners.py
```
- Place the printed checkerboard target flat in the sorting dish
- Room lights ON, backlight OFF
- Output saved to `camera_calibration\output\`
- Check `corners_numbered.png` — should show RED circles on 30 corners with C0/C1/C3/C4 labelled

### Step 2 — Robot grid scan (AUTOMATED — preferred)
```
& "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step2_robot_grid_scan.py
```
**Before running:**
- Load `calibration_server.prg` into RC+ 7.0 and run it (F5)
  - NOT `robot_server.prg` — that one does not have WHERE or MOVETO
- Make sure robot Z is at a safe working height (script never changes Z)

**What it does:**
1. You manually jog to 3 anchor corners (C0, C1, C3) using the teach pendant — press ENTER each time
2. Script computes the full 6×5 grid from those 3 anchors
3. Robot automatically visits all 30 corners via MOVETO commands
4. Robot XY is recorded at each corner (from the MOVETO reply)
5. Homography H is computed from 30 pixel↔robot pairs
6. H saved to `output\` and `C:\LabAutomation\config\calibration_overhead.npy`

### Step 2 (manual fallback) — Enter robot XY by hand
```
& "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step2_compute_homography.py
```
- Use only if the robot grid scan fails or calibration_server.prg is unavailable
- Jog robot manually to 4 corners, type X/Y values from pendant display when prompted

### Step 3 — Verify accuracy
```
& "C:\Users\TejasKumarLeelavathi\miniconda3\envs\cib_env\python.exe" camera_calibration\step3_verify.py
```
- Click any point in the overhead image → predicted robot XY is shown
- Jog robot to that XY → check if pipette lands on that spot
- Error should be < 0.5 mm

## When to recalibrate
- Camera is moved or re-mounted
- Robot origin changes
- Dish fixture is repositioned
- Pick accuracy degrades noticeably

## Output folder
All intermediate files saved to `camera_calibration/output/`

| File | Contents |
|------|----------|
| `overhead_test.png` | Step 0 overhead camera test image |
| `eoa_test.png` | Step 0 EoA camera test image |
| `camera_serials.json` | Camera serial numbers and IPs |
| `raw_capture.png` | Overhead image from Step 1 |
| `corners_detected.png` | Image with all 30 corners drawn |
| `corners_numbered.png` | Image with key corners labelled (use as jog guide) |
| `corners.json` | Pixel coordinates of all 30 corners |
| `calibration_pairs.json` | All 30 pixel↔robot pairs + RMSE (from Step 2 scan) |
| `calibration_overhead.npy` | Homography matrix H (3×3 float32) |
| `reprojection_check.png` | Accuracy check: green=good, orange=ok, red=bad |
