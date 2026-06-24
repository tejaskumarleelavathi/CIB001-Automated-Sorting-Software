"""
test_bug_detection.py — Run bug detection on saved overhead images.

Reads every PNG/TIFF from output/overhead_images/, runs the same detection
pipeline as bug_detection_grey.py, and saves annotated results to
output/bugs_detected/ using the same filename.

Usage:
    conda run -n cib_env python hardware_tests/test_bug_detection.py
    conda run -n cib_env python hardware_tests/test_bug_detection.py --input output/overhead_images/overhead_20260619_161047_079.png

Logic lifted directly from:
    Bug_Detection/src/bug_detection_grey.py
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

# ── Tunables (match bug_detection_grey.py) ────────────────────────────────────
THRESHOLD_VALUE   = 120    # Grayscale pixel value; below this = dark bug (0-255)
MIN_BUG_AREA      = 300    # px^2 — ignore tiny specks
MAX_BUG_AREA      = 25000  # px^2 — ignore merged clusters / large debris
MORPH_KERNEL_SIZE = 5
MORPH_OPEN_ITER   = 1
DISH_MARGIN        = 0.91  # Fraction of dish radius used as interior mask
DISH_RADIUS_EXPAND = 1.03  # Expand detected radius to reach true dish outer edge
# ─────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent   # CIB_Software/
INPUT_DIR  = ROOT / "output" / "overhead_images"
OUTPUT_DIR = ROOT / "output" / "bugs_detected"


# ── Pipeline stages ───────────────────────────────────────────────────────────

def to_gray(img):
    if len(img.shape) == 2:
        return img   # already grayscale
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def find_bright_rect(gray):
    """Locate the backlit white panel."""
    _, mask = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY)
    kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 30))
    mask    = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    return cv2.boundingRect(max(cnts, key=cv2.contourArea))


def find_dish(gray, rect=None):
    """Hough Circle Transform to find the Petri dish. Returns (cx, cy, r)."""
    h, w = gray.shape
    if rect is not None:
        rx, ry, rw, rh = rect
        roi   = gray[ry:ry+rh, rx:rx+rw]
        min_r = int(min(rw, rh) * 0.35)
        max_r = int(min(rw, rh) * 0.52)
        ox, oy = rx, ry
    else:
        roi   = gray
        min_r = int(min(h, w) * 0.25)
        max_r = int(min(h, w) * 0.50)
        ox, oy = 0, 0

    blurred = cv2.GaussianBlur(roi, (13, 13), 3)
    circles = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1,
                               minDist=max(roi.shape),
                               param1=60, param2=28,
                               minRadius=min_r, maxRadius=max_r)
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        expanded_r = int(c[2] * DISH_RADIUS_EXPAND)
        return int(c[0]+ox), int(c[1]+oy), expanded_r

    # Fallback: use centre of bright rect or image centre
    if rect is not None:
        rx, ry, rw, rh = rect
        return rx+rw//2, ry+rh//2, int(min(rw, rh)*0.44)
    return w//2, h//2, int(min(h, w)*0.40)


def make_interior_mask(shape, cx, cy, radius):
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.circle(mask, (cx, cy), int(radius * DISH_MARGIN), 255, -1)
    return mask


def threshold_bugs(gray, mask):
    """Pixels BELOW THRESHOLD_VALUE (dark bugs) become white."""
    _, bugs_white = cv2.threshold(gray, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY_INV)
    return cv2.bitwise_and(bugs_white, bugs_white, mask=mask)


def morph_clean(binary):
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE))
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, k, iterations=MORPH_OPEN_ITER)


def get_bugs(binary):
    cnts, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return cnts   # no area filter — same as final step in bug_detection_grey.py


def draw_result(img, bugs, cx, cy, radius):
    """Draw dish outline (orange), bug circles (green), centroids (red), count label."""
    out = img.copy() if len(img.shape) == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    # Dish outline
    cv2.circle(out, (cx, cy), radius, (0, 165, 255), 6)

    # Bugs
    for cnt in bugs:
        (x, y), r = cv2.minEnclosingCircle(cnt)
        cv2.circle(out, (int(x), int(y)), max(1, int(r)), (0, 255, 0), 2)
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            bx = int(M["m10"] / M["m00"])
            by = int(M["m01"] / M["m00"])
            cv2.circle(out, (bx, by), 5, (0, 0, 255), -1)

    # Count label
    label = f"Bugs detected: {len(bugs)}"
    sc    = max(1.0, img.shape[1] / 1500)
    th    = max(2, int(sc * 2))
    (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, sc, th)
    pad = int(sc * 12)
    cv2.rectangle(out, (pad, pad), (pad*2+tw, pad*2+tht), (0, 0, 0), -1)
    cv2.putText(out, label, (int(pad*1.5), pad+tht),
                cv2.FONT_HERSHEY_SIMPLEX, sc, (0, 255, 0), th)
    return out


# ── Per-image pipeline ────────────────────────────────────────────────────────

def detect(image_path: Path) -> tuple[int, np.ndarray]:
    """
    Run full detection on one image.
    Returns (bug_count, annotated_bgr_image).
    """
    img = cv2.imread(str(image_path))
    if img is None:
        raise FileNotFoundError(f"Cannot load: {image_path}")

    gray   = to_gray(img)
    rect   = find_bright_rect(gray)
    cx, cy, radius = find_dish(gray, rect)
    mask   = make_interior_mask(gray.shape, cx, cy, radius)
    thresh = threshold_bugs(gray, mask)
    cleaned = morph_clean(thresh)
    bugs   = get_bugs(cleaned)
    result = draw_result(img, bugs, cx, cy, radius)
    return len(bugs), result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bug detection on overhead images")
    parser.add_argument(
        "--input", "-i", nargs="*",
        help="Specific image file(s) to process. Default: all images in output/overhead_images/",
    )
    args = parser.parse_args()

    if args.input:
        images = [Path(p) for p in args.input]
    else:
        images = sorted(INPUT_DIR.glob("overhead_*.png")) + sorted(INPUT_DIR.glob("overhead_*.tiff"))

    if not images:
        print(f"No images found in {INPUT_DIR}")
        print("Run main.py first to capture some overhead images.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Processing {len(images)} image(s)")
    print(f"  Input:  {INPUT_DIR}")
    print(f"  Output: {OUTPUT_DIR}")
    print()

    total_bugs = 0
    for img_path in images:
        try:
            count, annotated = detect(img_path)
            out_path = OUTPUT_DIR / img_path.name   # same filename
            cv2.imwrite(str(out_path), annotated, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            total_bugs += count
            print(f"  {img_path.name}  →  {count} bug(s)  →  saved {out_path.name}")
        except Exception as exc:
            print(f"  {img_path.name}  →  ERROR: {exc}")

    print()
    print(f"Done. Total bugs detected across all images: {total_bugs}")
    print(f"Results saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
