#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annotate.py — Minimal OpenCV bounding-box annotator for EoA images
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Built to replace LabelImg after it crashed on this machine (a known
PyQt5-sip/newer-PyQt5 type-mismatch bug in its zoom handler). Runs
entirely on OpenCV's own window + mouse callback, no Qt at all.

Run with the cib_ml conda environment (has opencv-python + numpy
installed at a short path -- see the project chat history for why the
project's own .venv can't be used for this):

    C:\\Users\\<you>\\miniconda3\\envs\\cib_ml\\python.exe software_tests\\yolo_eoa\\annotate.py

Controls:
    Click + drag           Draw a new box
    Click inside a box     Select it (drawn thicker, white)
    B / H / D              Label the selected/just-drawn box BUG / HAIR / DEBRIS
    R                      Remove the selected box
    N                      Save this image's labels, go to next image
    P                      Save this image's labels, go to previous image
    Q or Esc                Save this image's labels, quit

Saves standard YOLO format (class_index cx cy w h, all normalized
0-1) to a .txt file next to each image -- exactly what split_dataset.py
expects. Boxes with no class assigned yet are dropped (with a console
warning) rather than saved, so leaving one half-drawn never corrupts
the label file.

Resumable: remembers your position in dataset/.annotate_state.txt, and
re-loads any image's existing .txt labels when you land on it (so
revisiting earlier images to fix mistakes is safe).
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _paths import DATASET_DIR  # noqa: E402

HERE = Path(__file__).parent
IMAGES_DIR = DATASET_DIR / "images"
CLASSES_FILE = HERE / "classes.txt"
STATE_FILE = DATASET_DIR / ".annotate_state.txt"

MAX_DISPLAY_DIM = 1100
WINDOW_NAME = "EoA annotator  (drag=box, click=select, B/H/D=label, R=remove, N/P=save+move, Q=quit)"

CLASS_COLORS = [
    (0, 200, 0),      # index 0 (BUG)      -- green
    (0, 210, 210),    # index 1 (HAIR)     -- yellow
    (60, 60, 220),    # index 2 (DEBRIS)   -- red
]
SELECTED_COLOR = (255, 255, 255)
PENDING_COLOR = (255, 200, 0)
KEY_TO_CLASS = {ord("b"): 0, ord("h"): 1, ord("d"): 2}


class Box:
    __slots__ = ("x1", "y1", "x2", "y2", "cls")

    def __init__(self, x1: float, y1: float, x2: float, y2: float, cls: int | None = None):
        self.x1, self.y1 = min(x1, x2), min(y1, y2)
        self.x2, self.y2 = max(x1, x2), max(y1, y2)
        self.cls = cls

    def contains(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


class Annotator:
    def __init__(self, class_names: list[str]):
        self.class_names = class_names
        self.files = sorted(IMAGES_DIR.glob("*.jpg")) + sorted(IMAGES_DIR.glob("*.png"))
        if not self.files:
            raise SystemExit(f"No images found in {IMAGES_DIR}")
        self.index = self._load_state()
        self.boxes: list[Box] = []
        self.selected: int | None = None
        self.scale = 1.0
        self.orig_img: np.ndarray | None = None
        self.drag_start: tuple[float, float] | None = None
        self.drag_current: tuple[float, float] | None = None
        self._load_image(self.index)

    # ── State persistence ───────────────────────────────────────────
    def _load_state(self) -> int:
        if STATE_FILE.exists():
            try:
                idx = int(STATE_FILE.read_text().strip())
                if 0 <= idx < len(self.files):
                    return idx
            except ValueError:
                pass
        return 0

    def _save_state(self) -> None:
        STATE_FILE.write_text(str(self.index))

    # ── Image / label I/O ───────────────────────────────────────────
    def _load_image(self, index: int) -> None:
        path = self.files[index]
        img = cv2.imread(str(path))
        if img is None:
            raise SystemExit(f"Could not read {path}")
        self.orig_img = img
        h, w = img.shape[:2]
        self.scale = min(MAX_DISPLAY_DIM / w, MAX_DISPLAY_DIM / h, 1.0)
        self.boxes = self._load_labels(path, w, h)
        self.selected = None

    def _label_path(self, image_path: Path) -> Path:
        return image_path.with_suffix(".txt")

    def _load_labels(self, image_path: Path, img_w: int, img_h: int) -> list[Box]:
        label_path = self._label_path(image_path)
        boxes = []
        if label_path.exists():
            for line in label_path.read_text().splitlines():
                parts = line.split()
                if len(parts) != 5:
                    continue
                cls = int(parts[0])
                cx, cy, bw, bh = (float(p) for p in parts[1:])
                x1 = (cx - bw / 2) * img_w
                y1 = (cy - bh / 2) * img_h
                x2 = (cx + bw / 2) * img_w
                y2 = (cy + bh / 2) * img_h
                boxes.append(Box(x1, y1, x2, y2, cls))
        return boxes

    def _save_labels(self) -> None:
        path = self.files[self.index]
        h, w = self.orig_img.shape[:2]
        label_path = self._label_path(path)
        lines = []
        dropped = 0
        for box in self.boxes:
            if box.cls is None:
                dropped += 1
                continue
            cx = (box.x1 + box.x2) / 2 / w
            cy = (box.y1 + box.y2) / 2 / h
            bw = (box.x2 - box.x1) / w
            bh = (box.y2 - box.y1) / h
            lines.append(f"{box.cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        if lines:
            label_path.write_text("\n".join(lines) + "\n")
        elif label_path.exists():
            label_path.unlink()  # no labeled boxes left -- don't leave a stale/empty file
        if dropped:
            print(f"  (dropped {dropped} unlabeled box(es) on {path.name} -- press B/H/D before moving on)")

    # ── Coordinate conversion ───────────────────────────────────────
    def to_orig(self, dx: float, dy: float) -> tuple[float, float]:
        return dx / self.scale, dy / self.scale

    def to_display(self, ox: float, oy: float) -> tuple[int, int]:
        return int(ox * self.scale), int(oy * self.scale)

    # ── Rendering ───────────────────────────────────────────────────
    def render(self) -> np.ndarray:
        h, w = self.orig_img.shape[:2]
        disp = cv2.resize(self.orig_img, (int(w * self.scale), int(h * self.scale)))
        for i, box in enumerate(self.boxes):
            p1 = self.to_display(box.x1, box.y1)
            p2 = self.to_display(box.x2, box.y2)
            if box.cls is None:
                color, thickness = PENDING_COLOR, 2
            elif i == self.selected:
                color, thickness = SELECTED_COLOR, 3
            else:
                color, thickness = CLASS_COLORS[box.cls], 2
            cv2.rectangle(disp, p1, p2, color, thickness)
            label = self.class_names[box.cls] if box.cls is not None else "?"
            cv2.putText(disp, label, (p1[0], max(0, p1[1] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        if self.drag_start and self.drag_current:
            cv2.rectangle(disp, self.drag_start, self.drag_current, PENDING_COLOR, 1)
        path = self.files[self.index]
        status = f"[{self.index + 1}/{len(self.files)}] {path.name}  boxes={len(self.boxes)}"
        cv2.rectangle(disp, (0, 0), (disp.shape[1], 22), (0, 0, 0), -1)
        cv2.putText(disp, status, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return disp

    # ── Mouse ───────────────────────────────────────────────────────
    def on_mouse(self, event, x, y, flags, param) -> None:
        ox, oy = self.to_orig(x, y)
        if event == cv2.EVENT_LBUTTONDOWN:
            hit = next((i for i in reversed(range(len(self.boxes))) if self.boxes[i].contains(ox, oy)), None)
            if hit is not None:
                self.selected = hit
                self.drag_start = None
            else:
                self.drag_start = (x, y)
                self.drag_current = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_start:
            self.drag_current = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.drag_start:
            x1, y1 = self.to_orig(*self.drag_start)
            x2, y2 = ox, oy
            self.drag_start = None
            self.drag_current = None
            if abs(x2 - x1) > 3 and abs(y2 - y1) > 3:  # ignore accidental clicks
                self.boxes.append(Box(x1, y1, x2, y2))
                self.selected = len(self.boxes) - 1

    # ── Main loop ───────────────────────────────────────────────────
    def run(self) -> None:
        cv2.namedWindow(WINDOW_NAME)
        cv2.setMouseCallback(WINDOW_NAME, self.on_mouse)
        print("Controls: drag=new box | click=select | B/H/D=label | R=remove | N/P=save+move | Q=quit")
        while True:
            cv2.imshow(WINDOW_NAME, self.render())
            key = cv2.waitKey(20) & 0xFF

            if key in KEY_TO_CLASS and self.selected is not None:
                self.boxes[self.selected].cls = KEY_TO_CLASS[key]
            elif key == ord("r") and self.selected is not None:
                del self.boxes[self.selected]
                self.selected = None
            elif key == ord("n"):
                self._save_labels()
                self.index = min(self.index + 1, len(self.files) - 1)
                self._save_state()
                self._load_image(self.index)
            elif key == ord("p"):
                self._save_labels()
                self.index = max(self.index - 1, 0)
                self._save_state()
                self._load_image(self.index)
            elif key in (ord("q"), 27):
                self._save_labels()
                break
        cv2.destroyAllWindows()


def main() -> None:
    if not CLASSES_FILE.exists():
        raise SystemExit(f"Missing {CLASSES_FILE}")
    class_names = [line.strip() for line in CLASSES_FILE.read_text().splitlines() if line.strip()]
    Annotator(class_names).run()


if __name__ == "__main__":
    sys.exit(main())
