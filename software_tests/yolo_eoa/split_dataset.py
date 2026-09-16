#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_dataset.py — Train/val/test split for annotated EoA images
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Run AFTER annotating images in dataset/images/ (see annotate.py). Only
images that actually have a matching YOLO .txt label file are included
-- anything still un-annotated is left in place and reported, not
silently dropped.

Three-way split, not just train/val: val is used DURING training to
monitor progress (ultralytics sees it, can indirectly influence which
checkpoint you pick), test is held out completely and only touched by
evaluate.py after training is done, for an honest accuracy number.

Produces the standard ultralytics YOLO layout:
    dataset/images/{train,val,test}/
    dataset/labels/{train,val,test}/
    dataset/data.yaml

Safe to re-run: it always re-derives the split from whatever is
currently annotated in dataset/images/ (plus anything already split
from a previous run), so annotating in batches and re-running this
periodically as you go is fine. The random seed is fixed, so a given
annotated set always produces the same split -- images don't leak
between train/val and test just because you re-ran this after
annotating a few more.

Usage:
    python software_tests/yolo_eoa/split_dataset.py [--val-fraction 0.15] [--test-fraction 0.15]
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _paths import DATASET_DIR  # noqa: E402

HERE = Path(__file__).parent
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
CLASSES_FILE = HERE / "classes.txt"
SPLIT_NAMES = ("train", "val", "test")
SEED = 42  # fixed -- same split every re-run for a given annotated set


def find_annotated_pairs() -> list[tuple[Path, Path]]:
    """(image, label) pairs from every split location -- flat pool plus
    any already-split images, so re-running after annotating more
    images folds them into a fresh, consistent split."""
    pairs = []
    search_dirs = [IMAGES_DIR] + [IMAGES_DIR / name for name in SPLIT_NAMES]
    for img_dir in search_dirs:
        if not img_dir.is_dir():
            continue
        for img_path in list(img_dir.glob("*.jpg")) + list(img_dir.glob("*.png")):
            label_path = img_path.with_suffix(".txt")
            if not label_path.exists():
                # Also check the mirrored labels/ subfolder (in case this
                # image was already split by a previous run).
                alt_label = LABELS_DIR / img_dir.name / f"{img_path.stem}.txt"
                if alt_label.exists():
                    label_path = alt_label
                else:
                    continue
            pairs.append((img_path, label_path))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--test-fraction", type=float, default=0.15)
    args = parser.parse_args()

    if not CLASSES_FILE.exists():
        raise SystemExit(f"Missing {CLASSES_FILE}")
    class_names = [line.strip() for line in CLASSES_FILE.read_text().splitlines() if line.strip()]

    pairs = find_annotated_pairs()
    total_images = len(list(IMAGES_DIR.glob("*.jpg"))) + len(list(IMAGES_DIR.glob("*.png")))
    un_annotated = total_images - len([p for p in pairs if p[0].parent == IMAGES_DIR])
    if not pairs:
        raise SystemExit(
            f"No annotated (image + .txt label) pairs found under {IMAGES_DIR}. "
            "Annotate first (see annotate.py)."
        )

    rng = random.Random(SEED)
    rng.shuffle(pairs)
    n_test = max(1, round(len(pairs) * args.test_fraction))
    n_val = max(1, round(len(pairs) * args.val_fraction))
    splits = {
        "test": pairs[:n_test],
        "val": pairs[n_test:n_test + n_val],
        "train": pairs[n_test + n_val:],
    }

    for split_name in SPLIT_NAMES:
        split_pairs = splits[split_name]
        img_dest = IMAGES_DIR / split_name
        lbl_dest = LABELS_DIR / split_name
        img_dest.mkdir(parents=True, exist_ok=True)
        lbl_dest.mkdir(parents=True, exist_ok=True)
        for img_path, label_path in split_pairs:
            img_target = img_dest / img_path.name
            lbl_target = lbl_dest / label_path.with_suffix(".txt").name
            if img_path.resolve() != img_target.resolve():
                shutil.move(str(img_path), str(img_target))
            if label_path.resolve() != lbl_target.resolve():
                shutil.move(str(label_path), str(lbl_target))

    data_yaml = DATASET_DIR / "data.yaml"
    data_yaml.write_text(
        "path: " + str(DATASET_DIR.resolve()).replace("\\", "/") + "\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        f"nc: {len(class_names)}\n"
        f"names: {class_names}\n"
    )

    for split_name in SPLIT_NAMES:
        print(f"{split_name.capitalize():6s}: {len(splits[split_name])} images -> {IMAGES_DIR / split_name}")
    print(f"Wrote: {data_yaml}")
    if un_annotated > 0:
        print(f"\nNote: {un_annotated} image(s) in {IMAGES_DIR} still have no label file "
              "(not un-annotated on purpose? annotate them and re-run this script).")


if __name__ == "__main__":
    main()
