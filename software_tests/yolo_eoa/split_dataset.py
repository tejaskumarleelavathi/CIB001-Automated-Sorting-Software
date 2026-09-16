#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_dataset.py — Train/val split for annotated EoA images
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

Run AFTER annotating images in dataset/images/ with LabelImg (see
prepare_dataset.py). Only images that actually have a matching YOLO
.txt label file are included -- anything still un-annotated is left in
place and reported, not silently dropped.

Produces the standard ultralytics YOLO layout:
    dataset/images/train/, dataset/images/val/
    dataset/labels/train/, dataset/labels/val/
    dataset/data.yaml

Safe to re-run: it always re-derives the split from whatever is
currently annotated in dataset/images/ (plus anything already split
into train/val from a previous run), so annotating in batches and
re-running this periodically as you go is fine.

Usage:
    python software_tests/yolo_eoa/split_dataset.py [--val-fraction 0.15]
"""
from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

HERE = Path(__file__).parent
DATASET_DIR = HERE / "dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
CLASSES_FILE = HERE / "classes.txt"
SEED = 42  # fixed -- same split every re-run for a given annotated set


def find_annotated_pairs() -> list[tuple[Path, Path]]:
    """(image, label) pairs from every split location -- flat pool plus
    any already-split train/val images, so re-running after annotating
    more images folds them into a fresh, consistent split."""
    pairs = []
    search_dirs = [IMAGES_DIR, IMAGES_DIR / "train", IMAGES_DIR / "val"]
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
            "Annotate with LabelImg first (see prepare_dataset.py)."
        )

    rng = random.Random(SEED)
    rng.shuffle(pairs)
    n_val = max(1, round(len(pairs) * args.val_fraction))
    val_pairs = pairs[:n_val]
    train_pairs = pairs[n_val:]

    for split_name, split_pairs in (("train", train_pairs), ("val", val_pairs)):
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
        f"nc: {len(class_names)}\n"
        f"names: {class_names}\n"
    )

    print(f"Train: {len(train_pairs)} images -> {IMAGES_DIR / 'train'}")
    print(f"Val:   {len(val_pairs)} images -> {IMAGES_DIR / 'val'}")
    print(f"Wrote: {data_yaml}")
    if un_annotated > 0:
        print(f"\nNote: {un_annotated} image(s) in {IMAGES_DIR} still have no label file "
              "(not un-annotated on purpose? annotate them and re-run this script).")


if __name__ == "__main__":
    main()
