"""
cib/ui/theme.py — Centralized colors and style helpers for the production UI.

Visual direction (operator's choice, 2026-08-27): "modern lab software"
(Opentrons-inspired) — white/light-grey background, card-grouped sections
with soft borders and rounded corners, one calm accent color for primary
actions, color otherwise reserved for status (state badge, safety banner,
alarms) per standard HMI practice. Before this file, every screen hand-wrote
its own hex-color .setStyleSheet() calls (see cib/ui/main_window.py's
original _STATE_COLOURS + per-button styles) — this consolidates that into
one place so new screens/widgets don't re-invent the palette.
"""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget

from cib.core.state_machine import State


# ── Palette ──────────────────────────────────────────────────────────────────

BACKGROUND      = "#f7f8fa"   # app/window background — light grey, not stark white
CARD_BACKGROUND = "#ffffff"
CARD_BORDER     = "#e2e5e9"
TEXT_PRIMARY    = "#1f2328"
TEXT_SECONDARY  = "#6b7280"
DIVIDER         = "#e2e5e9"

ACCENT          = "#2563eb"   # primary action color (START, active tab/tile)
ACCENT_HOVER    = "#3b76f0"
ACCENT_DISABLED = "#c7d2fe"

# Status colors — reserved for state/alarm meaning only (never decorative).
STATUS_GREY   = "#8a94a3"
STATUS_GREEN  = "#16a34a"
STATUS_BLUE   = "#2563eb"
STATUS_AMBER  = "#d97706"
STATUS_RED    = "#dc2626"
STATUS_PURPLE = "#7c3aed"

# (label, background, foreground) per state — replaces main_window.py's
# original _STATE_COLOURS dict, same meanings (architecture/ui.md).
STATE_COLOURS: dict[State, tuple[str, str, str]] = {
    State.INITIALIZING:    ("INITIALIZING",     STATUS_GREY,   "white"),
    State.IDLE:            ("IDLE — READY",     STATUS_GREEN,  "white"),
    State.RUNNING:         ("RUNNING",          STATUS_BLUE,   "white"),
    State.CYCLE_STOPPING:  ("STOPPING…",        STATUS_AMBER,  "white"),
    State.E_STOP:          ("E-STOP",           STATUS_RED,    "white"),
    State.PROTECTIVE_STOP: ("PROTECTIVE STOP",  STATUS_AMBER,  "white"),
    State.FAULT:           ("FAULT",            STATUS_PURPLE, "white"),
}


# ── Reusable style fragments ─────────────────────────────────────────────────

def window_stylesheet() -> str:
    """Base stylesheet applied to the top-level window/QStackedWidget host."""
    return f"""
        QWidget {{
            background-color: {BACKGROUND};
            color: {TEXT_PRIMARY};
            font-family: "Segoe UI", "Inter", sans-serif;
        }}
    """


def card_stylesheet() -> str:
    """A rounded, bordered panel — the basic grouping unit for every screen."""
    return f"""
        background-color: {CARD_BACKGROUND};
        border: 1px solid {CARD_BORDER};
        border-radius: 10px;
    """


def primary_button_stylesheet() -> str:
    """The one accent-colored button per screen (START, primary action)."""
    return f"""
        QPushButton {{
            background-color: {ACCENT};
            color: white;
            border: none;
            border-radius: 8px;
            padding: 4px;
        }}
        QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
        QPushButton:disabled {{ background-color: {ACCENT_DISABLED}; color: #eef2ff; }}
    """


def secondary_button_stylesheet() -> str:
    """Neutral outlined button — everything that isn't the primary action."""
    return f"""
        QPushButton {{
            background-color: {CARD_BACKGROUND};
            color: {TEXT_PRIMARY};
            border: 1px solid {CARD_BORDER};
            border-radius: 8px;
            padding: 4px;
        }}
        QPushButton:hover {{ background-color: {BACKGROUND}; }}
        QPushButton:disabled {{ color: {TEXT_SECONDARY}; background-color: {BACKGROUND}; }}
    """


def status_button_stylesheet(hex_color: str, hover_hex: str) -> str:
    """For buttons that carry status meaning (STOP=amber, ABORT=red, etc.)."""
    return f"""
        QPushButton {{
            background-color: {hex_color};
            color: white;
            border: none;
            border-radius: 8px;
            padding: 4px;
        }}
        QPushButton:hover {{ background-color: {hover_hex}; }}
        QPushButton:disabled {{ background-color: #d1d5db; color: #f3f4f6; }}
    """


def input_stylesheet() -> str:
    """
    Text input styling (QLineEdit) — consistent border/padding/focus state
    everywhere a screen takes typed input (field code, plate ID, starting
    well, etc.). Added 2026-09-01: these fields previously fell back to
    Qt's bare native rendering (thin single-pixel border, no padding),
    which read as under-designed next to the card/button styling
    everywhere else, and let a QGridLayout column with nothing else
    constraining it stretch the box far wider than the text inside it
    needed — see the matching setMaximumWidth() calls at each call site
    for the other half of that fix; this alone doesn't cap width.
    """
    return f"""
        QLineEdit {{
            background-color: {CARD_BACKGROUND};
            border: 1px solid {CARD_BORDER};
            border-radius: 6px;
            padding: 4px 8px;
            color: {TEXT_PRIMARY};
        }}
        QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
        QLineEdit:disabled {{ color: {TEXT_SECONDARY}; background-color: {BACKGROUND}; }}
    """


def state_badge_stylesheet(bg_hex: str, fg_hex: str) -> str:
    return (
        f"border-radius: 8px; background-color: {bg_hex}; "
        f"color: {fg_hex}; padding: 6px;"
    )


def apply_font(widget: QWidget, size: int = 12, bold: bool = False) -> None:
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    widget.setFont(f)
