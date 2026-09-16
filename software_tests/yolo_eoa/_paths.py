#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_paths.py — Shared filesystem locations for the YOLO EoA project
========================================================================
CIB001 Automated Sorting Prototype
California Institute for Biodiversity

DATASET_DIR and RUNS_DIR deliberately live OUTSIDE this repo, at a short
path. This machine's repo path is long enough (deeply nested company/
project folder names) that Windows' classic 260-character MAX_PATH
limit gets hit reliably once file names include split subfolders
(images/train/, labels/val/, etc.) or ultralytics' own nested run/
checkpoint paths -- confirmed live 2026-09-16 (a plain shutil.move
during split_dataset.py failed on a 263-character path). Long Path
support isn't enabled on this machine and changing that needs admin
rights, so the dataset and training artifacts just live somewhere
short instead. Nothing about the annotation data itself is meant to be
short-lived -- classes.txt and every script here still live in the
repo as normal, tracked source.
"""
from __future__ import annotations

from pathlib import Path

DATASET_DIR = Path(r"C:\cib_yolo_data\dataset")
RUNS_DIR = Path(r"C:\cib_yolo_data\runs")
