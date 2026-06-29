# Vision System — Architecture & Implementation Notes

**Date written:** 2026-06-24 (session 5)  
**Last updated:** 2026-06-25 (session 7)  
**Status:** Full pipeline implemented and wired end-to-end. Ring light control added. Backlight timed simultaneously with robot motion. VIEW mode (popup windows for testing) implemented. Visited-set specimen tracking implemented (v1). EoA calibration pending (one-time lab step). Full state machine (PICKED/DEBRIS/RETRY/FAILED) documented for v2.

---

## Overview — Two-Stage Vision

The sorting system uses two physically separate cameras for two distinct tasks:

| Stage | Camera | Purpose | Output |
|---|---|---|---|
| Stage 1 — Overhead | Lucid TRI245S-CC (serial 253102168, IP 172.16.9.21) | Detect all specimens in dish, pick one | Bug centroid in robot XY mm |
| Stage 3 — EoA | Lucid TRI245S-C (serial 253102169, IP 172.16.9.22) | Confirm it is a bug, find precise body centre | Body centre in robot XY mm |

**Why two stages?**

The overhead camera is ~500mm above the dish. At that distance it can locate bug positions but not reliably distinguish bugs from debris, and its centroid accuracy (after homography) is limited by pixel scale. The EoA camera is mounted on the robot arm and images the specimen from ~40–80mm away. At this distance it can:

1. **Confirm identity** — classify as BUG / HAIR / DEBRIS before committing to a pick
2. **Refine position** — find the body centre (dark thorax/abdomen pixels) at much higher pixel-per-mm resolution. This matters because a centroid taken from the whole blob (body + wings) often falls on a wing — picking from a wing breaks it.

---

## Orchestrator cycle (implemented)

```
── Program startup ───────────────────────────────────────────────────────────
  robot.connect() + camera_overhead.connect() + camera_eoa.connect()
  lighting.connect() → backlight ON
  robot.ring_light_on()   ← output 12 ON, stays on for entire session

── Operator presses START ────────────────────────────────────────────────────
  _visited_centroids.clear()
  backlight already ON from startup

── _run_cycle() — repeats until dish empty or operator stops ─────────────────
  Step 1   camera_overhead.capture()            ← backlight ON
  Step 2   run_in_executor → run_overhead_detection(frame)
               → VisionOverheadResultEvent
                     centroids_robot_xy: list[(x_mm, y_mm)]
                     centroids_px:       list[(px, py)]        ← paired 1:1
                     annotated_frame:    np.ndarray            ← always populated
                     annotated_frame_path: str                 ← saved to cib/output/overhead_images/
                     sample_count: int

  Step 3   filter visited centroids → sort bottom-left first
           select target → mark visited immediately

  Step 3b  [VIEW=True] draw_target_highlight(annotated_frame, target_px)
           run_in_executor → cv2.imshow 3 s   ← all bugs green, target cyan crosshair

  Step 4   asyncio.gather(
               lighting.set_backlight(False),       ← starts simultaneously
               robot.move_to_image_site(x, y),      ← 30–60 s at 5% speed
           )
           # backlight off in <1 s, robot arrives seconds later → EoA with no backlight

  Step 5   camera_eoa.capture()                 ← backlight OFF, ring light ON
  Step 6   run_in_executor → run_eoa_analysis(frame)
               → VisionEoAResultEvent
                     proceed_with_pick: bool
                     refined_x / refined_y: float | None
                     frame_path: str             ← saved to cib/output/eoa_images/

  Step 6b  [VIEW=True] run_in_executor → cv2.imshow(frame_path) 3 s

  Step 7   if not proceed_with_pick:
               asyncio.gather(lighting.set_backlight(True), robot.move_to_safe_park())
               → next cycle

  Step 8   refined_x = eoa_result.refined_x or target_x
           refined_y = eoa_result.refined_y or target_y
           [TODO: robot.execute_pick_place — currently goes home after EoA]
           asyncio.gather(lighting.set_backlight(True), robot.move_to_safe_park())
           # backlight on in <1 s during robot travel → ready for next overhead

  Step 9   → back to Step 1

── Operator presses SHUTDOWN ─────────────────────────────────────────────────
  robot.ring_light_off()   ← output 12 OFF
  lighting.set_backlight(False)
  lighting.disconnect()
```

**Key timing design:** `asyncio.gather(backlight_cmd, robot_move)` starts both simultaneously. Backlight settles in under 1 second; robot travels for 30–60 seconds. Zero wasted wait time at either transition.

Both vision functions run in `loop.run_in_executor(None, ...)` (thread pool) so they don't block the asyncio loop during CPU-intensive OpenCV work.

---

## Stage 1 — Overhead detection (`cib/vision/pipeline.py`)

### Algorithm (Method C — no minimum area filter)

1. Grayscale conversion
2. Locate backlit white panel (brightness threshold 70 + largest contour)
3. Detect Petri dish circle (Hough Circle Transform on panel ROI)
4. Create interior dish mask (91% of dish radius, trims opaque rim)
5. **Global inverse threshold** — pixels below `THRESHOLD_VALUE=120` are specimens
6. **Morphological open** (5×5 ellipse, 1 iter) — removes single-pixel noise
7. Contour detection — **NO minimum area lower bound** — keeps tiny specimens
8. Filter: only contours with area ≤ `MAX_BUG_AREA=25,000 px²` (rejects merged clusters)
9. Compute centroids → transform via overhead calibration homography → robot XY mm
10. Annotate frame, save to disk if `save_dir` configured

### Key constants

| Constant | Value | Notes |
|---|---|---|
| `THRESHOLD_VALUE` | 120 | Global inverse threshold — pixels below = specimen |
| `MIN_BUG_AREA` | 1 px² | No lower bound — keeps tiny specimens |
| `MAX_BUG_AREA` | 25,000 px² | Rejects merged blob clusters |
| `MORPH_KERNEL_SIZE` | 5 | Morphological open kernel diameter |
| `DISH_MARGIN` | 0.91 | Interior mask = 91% of detected dish radius |

### Return type

```python
VisionOverheadResultEvent(
    centroids_robot_xy: list[tuple[float, float]],  # robot XY mm — paired 1:1 with centroids_px
    centroids_px:       list[tuple[float, float]],  # pixel coords — used by VIEW target highlight
    annotated_frame:    np.ndarray,                 # always populated (even when save_dir=None)
    annotated_frame_path: str = "",                 # path to saved PNG/TIFF, or ""
    sample_count: int = 0,
)
```

`centroids_px[i]` is the pixel centroid corresponding to `centroids_robot_xy[i]`. The orchestrator uses this pairing to find the pixel location of the selected target and draw the cyan crosshair for VIEW mode.

### `draw_target_highlight(annotated, target_px)` — new in session 7

Public function in `pipeline.py`. Takes an already-annotated frame and draws a cyan circle + crosshair at `target_px`. Marker size scales with image dimensions (images are 5320×4600 px). Called by the orchestrator after target selection when `VIEW=True`.

### Calibration

- File: `C:\LabAutomation\config\calibration_overhead.npy` (3×3 float32 homography H)
- **Calibrated:** 2026-06-17, RMSE = 0.043 mm
- Loaded via `cib.vision.calibration.load_calibration()`
- Falls back to raw pixel coordinates if file not found (dev mode only)
- **Recalibrate if:** camera moved, robot origin changed, dish fixture repositioned

---

## Stage 3 — EoA classification + body centre (`cib/vision/eoa.py`)

### Why body centre, not blob centroid

The blob geometric centroid (centre of all segmented pixels) can fall on a semi-transparent wing. Wing attachment point = wing breakage. We need the centre of the **dark body** (thorax/abdomen), not the whole blob.

Implementation:
- Threshold at `BODY_DARK_THRESH=70` (tighter than detection threshold 90)
- Wings are ~150–210 gray — they get weight zero at this threshold
- Use **intensity-weighted centroid**: `weight = (255 - pixel_gray)`, so the darkest part (densest body region) pulls the centroid
- Take only the largest connected component of body pixels (avoids detached legs pulling centroid sideways)

### Algorithm

1. Grayscale + **Otsu inverse threshold** (auto-adapts to any backlight level)
2. **Coverage guard**: if >45% of frame is foreground → backlight off or bad image → return `proceed=False`
3. Morphological close (15×15 ellipse, 2 iter) — bridges body↔wing gaps
4. Find all external contours ≥ 50 px²
5. **Center-first candidate ranking**: sort blobs into concentric rings (15% / 30% / 50% / 100% of frame half-diagonal). Within each ring: largest blob first. The arm holds the specimen below the camera centre.
6. **Iterate candidates**: for each, compute intensity features and classify:
   - Skip HAIR (aspect > 1.6 AND solidity < 0.45)
   - Skip DEBRIS (dark_ratio < 0.25, or chroma > 45, or too small, or wrong solidity range)
   - **Stop at first BUG** → this is the pick target
7. **Body centre**: intensity-weighted centroid of pixels < 70 gray within largest dark-pixel component
8. **EoA calibration**: transform body centre pixel → robot XY mm
   - If `calibration_eoa.npy` not found: `refined_x=None, refined_y=None` → orchestrator falls back to overhead centroid (safe default)
9. Save annotated frame to `eoa_save_dir` if configured

### Classification rules (in priority order)

| Priority | Rule | Label |
|---|---|---|
| 1 | aspect_ratio > 1.6 AND solidity < 0.45 | HAIR |
| 2 | dark_pixel_ratio < 0.25 | DEBRIS (not a dark insect) |
| 3 | mean_chroma_variance > 45 | DEBRIS (colored, not neutral insect) |
| 4 | equiv_diameter < 25 px | DEBRIS (too small) |
| 5 | solidity > 0.92 | DEBRIS (perfectly smooth, no appendages) |
| 6 | solidity < 0.18 | DEBRIS (too fragmented) |
| 7 | solidity in [0.18, 0.75] | BUG (appendage concavity confirmed) |
| 8 | solidity in (0.75, 0.92] AND defects >= 2 | BUG (compact body with appendage notches) |
| else | — | DEBRIS (smooth compact blob, no appendages) |

### Key constants

| Constant | Value | Notes |
|---|---|---|
| `BODY_DARK_THRESH` | 70 | Tighter threshold for body-only pixels (wings: 150–210) |
| `DARK_PIXEL_THRESH` | 90 | Detection threshold for dark-pixel ratio gate |
| `BLOB_DARK_RATIO_MIN` | 0.25 | Min fraction of dark pixels for bug candidate |
| `BLOB_CHROMA_MAX` | 45 | Max BGR chroma — rejects colored debris |
| `MAX_FG_COVERAGE` | 0.45 | Coverage > 45% = bad lighting → no-go |
| `HAIR_ASPECT_THRESH` | 1.6 | Aspect ratio for hair/leg/filament rejection |
| `HAIR_SOLIDITY_MAX` | 0.45 | Solidity upper bound for hair classification |
| `BUG_SOLIDITY_MIN` | 0.18 | Lower solidity bound (too fragmented = not a bug) |
| `BUG_SOLIDITY_MAX` | 0.92 | Upper solidity bound (too smooth = no appendages) |
| `BUG_HISOL_THRESH` | 0.75 | Above this, require defect evidence |
| `BUG_MIN_DEFECTS` | 2 | Min significant convexity defects at high solidity |
| `MIN_DEFECT_DEPTH_PX` | 5.0 | Min notch depth to count as appendage evidence |
| `CENTER_SEARCH_FRACS` | (0.15, 0.30, 0.50, 1.0) | Expanding ring fracs of frame half-diagonal |

### Return type

```python
VisionEoAResultEvent(
    proceed_with_pick: bool,        # True only when a real bug is confirmed
    refined_x: float | None,       # body centre robot X mm (None if no EoA calibration)
    refined_y: float | None,       # body centre robot Y mm
    frame_path: str,                # path to saved annotated frame, or ""
)
```

### Calibration

- File: `C:\LabAutomation\config\calibration_eoa.npy` (3×3 float32 homography H)
- **NOT YET CALIBRATED** — needs one-time lab step (same procedure as overhead)
- Loaded via `cib.vision.calibration.load_calibration(EOA_CALIBRATION_PATH)`
- If not found: `refined_x=None, refined_y=None` — orchestrator uses overhead centroid
- **What EoA calibration maps**: EoA pixel (px, py) → robot XY mm (absolute, same space as overhead)
- **Recalibrate if:** EoA camera repositioned on arm, robot kinematics changed

---

## Calibration (`cib/vision/calibration.py`)

```python
# Overhead calibration (completed 2026-06-17, RMSE=0.043mm)
from cib.vision.calibration import load_calibration
cal = load_calibration()                          # loads calibration_overhead.npy
rx, ry = cal.pixel_to_robot(px, py)              # pixel -> robot XY mm

# EoA calibration (pending)
from cib.vision.calibration import load_calibration, EOA_CALIBRATION_PATH
cal_eoa = load_calibration(EOA_CALIBRATION_PATH)  # loads calibration_eoa.npy
rx, ry = cal_eoa.pixel_to_robot(bc_px, bc_py)    # body centre pixel -> robot XY mm
```

Constants in `calibration.py`:
```python
DEFAULT_H_PATH      = r"C:\LabAutomation\config\calibration_overhead.npy"
EOA_CALIBRATION_PATH = r"C:\LabAutomation\config\calibration_eoa.npy"
```

`CalibrationData.pixel_to_robot()` uses `cv2.perspectiveTransform` with the 3×3 H matrix. Works for both cameras — the H matrix encodes the different geometry for each.

---

## Annotated frame saving

| Frame type | Filename | Dev mode path | Prod mode path | Controlled by |
|---|---|---|---|---|
| Overhead (debug) | `overhead_{YYYYMMDD}_{HHMMSS}_{ms}.{fmt}` | `cib/output/overhead_images/` | `C:\LabAutomation\data\overhead_images\` | `overhead_save_images: true` in machine_config.yaml |
| EoA (archival) | `eoa_{YYYYMMDD}_{HHMMSS}_{ms}_{LABEL}.jpg` | `cib/output/eoa_images/` | `C:\LabAutomation\data\runs\` | always saved — `eoa_save_dir` always set in main.py |

Both dirs are created at startup by `main.py` using `mkdir(parents=True, exist_ok=True)`.

Overhead annotated frames show:
- Orange circle: detected Petri dish boundary
- Green circle + red dot: each detected bug (all specimens)
- Top-left banner: specimen count + timestamp
- **[VIEW=True only]** Cyan circle + crosshair + "TARGET" label: the selected bug for this cycle

EoA annotated frames show:
- Blob contour (green = BUG, grey = NONE)
- Convex hull (orange)
- Blob centroid (red dot)
- Body pixel overlay (cyan-tinted — the dark pixels used for body centre)
- Pipette target crosshair (cyan) at the intensity-weighted body centre
- Banner: label, size, solidity, dark_ratio, chroma, pipette target px coords, elapsed ms

## VIEW mode (`VIEW = True` in `main.py`)

Testing flag. When `True`, the orchestrator pops up two OpenCV windows per cycle:

1. **Overhead window** — annotated overhead with all bugs (green) and the selected target (cyan crosshair). Title shows bug count and target robot XY. Displayed for 3 seconds.
2. **EoA window** — the saved EoA annotated image showing classification result. Displayed for 3 seconds.

Both display calls run in `loop.run_in_executor` (thread pool) to avoid blocking the asyncio event loop. Images wider than 1200 px or taller than 800 px are scaled down to fit the screen.

**Set `VIEW = False` in production** — the 3-second pause per window would add 6+ seconds to every cycle.

## Ring light control

Ring light = robot digital output 12. Controlled via TCP commands to `sort_server.prg`.

| Command (Python → robot) | SPEL+ | Robot reply |
|---|---|---|
| `RING_ON\r\n` | `On 12` | `OK RING_ON\r\n` |
| `RING_OFF\r\n` | `Off 12` | `OK RING_OFF\r\n` |

Python API:
```python
await robot.ring_light_on()   # EpsonT3BDriver or MockRobotDriver
await robot.ring_light_off()
```

**Lifecycle:** `ring_light_on()` called in `_startup()` after all devices connect. `ring_light_off()` called in `OperatorShutdownEvent` handler before `lighting.disconnect()`. The ring light stays on for the entire session regardless of cycle state.

---

## Software test suite

All vision development and testing was done offline in `software_tests/` before wiring into the main pipeline.

### Offline test tools

| Script | Purpose |
|---|---|
| `software_tests/overhead_detection/method_c_no_area_filter.py` | Standalone overhead detection on saved images. CLI: `python method_c_no_area_filter.py [image/dir]` |
| `software_tests/eoa_classification/eoa_classifier.py` | Standalone EoA classifier on saved images. CLI with feature table, `--features-only`, `--save`, `--no-show` |
| `software_tests/eoa_classification/pipeline_demo.py` | Full end-to-end pipeline demo on saved images. CLI: `--overhead <path> --eoa <path>` |

### EoA classifier accuracy (test set, 2026-06-24)

Tested on 27 images from `Software/Images/` + `Software/EOA_images/`:

| Category | Count | Result |
|---|---|---|
| Real bug images | 18 | 18/18 BUG (100%) |
| Hair/follicle images | 2 | 2/2 NONE (100%) |
| Reddish debris images | 1 | 1/1 NONE (100%) |
| Backlight-off images | 5 | 5/5 NONE (100%) |
| Bad/noisy image | 1 | 1/1 NONE (100%) |

**Timing (on dev laptop):** 115–260 ms for zoomed single-specimen PNGs. 1–3.5 s for full-resolution LUCID JPEGs (large image, 100+ blobs). On the HMI, expect 200–500 ms per image.

### Pipeline demo output (`pipeline_demo.py`)

Runs all 4 stages on disk images and saves:
- `stage1_2_overhead_*.jpg` — dish with all detected bugs + selected bug crosshair (corner-first)
- `stage3_eoa_*_BUG.jpg` — EoA image with blob + classification result
- `stage4_body_*.jpg` — same frame with body-centre stage label

Output saved to `software_tests/eoa_classification/results/pipeline/`.

---

## Pixel intensity conventions (backlit imaging)

| Region | Typical gray value | Notes |
|---|---|---|
| Background (backlit panel) | 200–255 | Very bright |
| Bug wings (semi-transparent) | 150–210 | Transmits backlight partially |
| Bug body (thorax/abdomen) | 20–80 | Dark, melanized |
| Hair / filament | 100–180 | Medium, thin |
| Reddish debris | 100–160 | Medium, colored (amber, red) |

All cameras use **backlit** imaging. Backlight must be ON. The `MAX_FG_COVERAGE=0.45` guard in the EoA pipeline rejects images taken with backlight off (entire frame dark → Otsu maps it all to foreground → >45% coverage → return `proceed=False`).

---

## Threading model

```
Main thread: PyQt6 UI event loop

Background thread: asyncio event loop
    │
    ├─ camera_overhead.capture()              [run_in_executor — blocking arena_api]
    ├─ run_in_executor → run_overhead_detection()
    │       └─ returns VisionOverheadResultEvent (centroids_robot_xy, centroids_px,
    │                                             annotated_frame, annotated_frame_path)
    │
    ├─ [VIEW=True] run_in_executor → _show_image(annotated + target highlight, 3s)
    │
    ├─ asyncio.gather(
    │       lighting.set_backlight(False),         [fast — <1 s]
    │       robot.move_to_image_site(x, y),        [slow — 30–60 s]
    │   )
    │
    ├─ camera_eoa.capture()                   [run_in_executor — blocking arena_api]
    ├─ run_in_executor → run_eoa_analysis()
    │       ├─ classify (BUG / HAIR / DEBRIS)
    │       ├─ find_body_center() → (cx, cy, body_mask)
    │       └─ calibration_eoa.pixel_to_robot() → refined_x, refined_y
    │
    ├─ [VIEW=True] run_in_executor → _show_image_path(eoa frame_path, 3s)
    │
    └─ asyncio.gather(
            lighting.set_backlight(True),          [fast — <1 s]
            robot.move_to_safe_park(),             [slow — 30–60 s]
        )
```

Vision and VIEW display both run in the default thread pool executor (CPU-bound / blocking). Camera capture also runs in `run_in_executor` — `capture()` blocks on `arena_api` get_buffer with a 5 s timeout.

---

## Bug Tracking State Machine

### v1 — Visited set (implemented)

`Orchestrator` maintains `_visited_centroids: set[tuple[float, float]]` cleared at run start.

- **When marked:** immediately when the target centroid is selected (before robot moves), so it is never re-selected even if the camera still sees it on the next overhead frame.
- **Matching:** exact — coordinates are snapped to a 0.1mm grid (`round(v, 1)`) before storing and looking up. No radius. Calibration RMSE = 0.043mm, so the same physical spot always lands in the same 0.1mm cell. A radius would risk skipping adjacent bugs (which can be < 1mm apart).
- **Sort order:** bottom-left first — `sorted(unvisited, key=lambda c: (c[1], -c[0]))` where x is robot "toward dish bottom" and y is robot "toward dish right".
- **New overhead per cycle:** after every robot HOME, a fresh overhead image is taken. Newly-picked locations disappear naturally. The visited set only blocks EoA-rejected debris that _remains_ in the dish (would otherwise be re-targeted every cycle).

```python
# orchestrator.py
@staticmethod
def _snap(v):
    return round(v, 1)   # 0.1mm grid

def _is_visited(self, centroid):
    cx, cy = centroid
    return (self._snap(cx), self._snap(cy)) in self._visited_centroids

def _mark_visited(self, x, y):
    self._visited_centroids.add((self._snap(x), self._snap(y)))
```

### v2 — Per-centroid state machine (future, not yet implemented)

Replace the visited set with a dict mapping `(x, y)` → state. See PRD § 8.6a for the full requirements. Quick reference:

| State | Set when | Retry? |
|---|---|---|
| `PICKED` | Overhead post-pick confirms location empty | Never |
| `DEBRIS` | EoA classifies as not a bug | Never |
| `RETRY` | EoA uncertain or pick failed | Yes — up to 3 total |
| `FAILED` | 3 retries exhausted | No — alert operator |

Retry pass: after all fresh centroids are exhausted, revisit all `RETRY`-state positions in their original sort order, incrementing the attempt counter each time.

---

## What still needs to be done

| Item | Blocking | Action |
|---|---|---|
| EoA calibration (`calibration_eoa.npy`) | Refined pick coords | Run calibration_eoa in lab — same checkerboard+robot-jog method as overhead. Script TBD. Until done: orchestrator falls back to overhead centroid. |
| `execute_pick_place()` in robot driver | Actual picking | Currently a TODO placeholder that just goes home. Wire pipette aspirate/dispense, robot move to well, dispense. |
| Full PyQt6 UI | Operator screens | Pre-run setup, run monitor, cycle stop/abort/continue buttons. |
| PLC + Flexi real drivers | Safety integration | Hardware confirmed working in bench tests — wire into real driver classes. |
| Bug tracking state machine (v2) | Full retry/debris logic | See PRD § 8.6a — implement after v1 visited-set is validated in the lab. |
