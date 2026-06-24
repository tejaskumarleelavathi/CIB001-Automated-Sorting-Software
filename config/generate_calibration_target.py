"""
Generate calibration_target.pdf — overhead camera checkerboard calibration sheet.

Specs:
  - 7 columns x 6 rows of squares  ->  6x5 inner corners (what OpenCV detects)
  - Each square: 20mm x 20mm
  - Total board: 140mm x 120mm

HOW TO PRINT:
  Open in Adobe Reader -> File -> Print -> Page Sizing = "Actual Size" (NOT "Fit")
  Measure the 100mm scale bar after printing. Must be exactly 100mm.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
import os

# ── Config ───────────────────────────────────────────────────────────────────
COLS      = 7        # columns of squares
ROWS      = 6        # rows of squares
SQUARE_MM = 20       # each square side in mm
OUT_FILE  = os.path.join(os.path.dirname(__file__), "calibration_target.pdf")
# ─────────────────────────────────────────────────────────────────────────────

PAGE_W, PAGE_H = A4          # 595.27 x 841.89 pt  (210mm x 297mm)
BOARD_W = COLS * SQUARE_MM * mm
BOARD_H = ROWS * SQUARE_MM * mm
MARGIN   = 25 * mm          # 25mm left margin

# Board top-left origin (from bottom-left of page — reportlab Y goes up)
BOARD_X = MARGIN
BOARD_Y = PAGE_H - 60*mm - BOARD_H   # 60mm from top for title


def build_pdf() -> None:
    c = canvas.Canvas(OUT_FILE, pagesize=A4)
    c.setTitle("CIB Calibration Target — Overhead Camera")

    # ── Title ─────────────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 13)
    c.drawString(MARGIN, PAGE_H - 25*mm,
                 "CIB Automated Sorter — Overhead Camera Calibration Target")

    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, PAGE_H - 35*mm,
                 f"Checkerboard: {COLS}x{ROWS} squares  |  "
                 f"Square: {SQUARE_MM}mm x {SQUARE_MM}mm  |  "
                 f"Inner corners for OpenCV: {COLS-1}x{ROWS-1}  |  "
                 f"Board: {COLS*SQUARE_MM}mm x {ROWS*SQUARE_MM}mm")

    # ── Checkerboard ──────────────────────────────────────────────────────────
    sq = SQUARE_MM * mm
    for row in range(ROWS):
        for col in range(COLS):
            x = BOARD_X + col * sq
            # reportlab Y=0 is bottom; row 0 should be at top of board
            y = BOARD_Y + (ROWS - 1 - row) * sq
            if (row + col) % 2 == 0:
                c.setFillColor(colors.black)
            else:
                c.setFillColor(colors.white)
            c.setStrokeColor(colors.black)
            c.setLineWidth(0)
            c.rect(x, y, sq, sq, fill=1, stroke=0)

    # Draw a thin outer border around the whole board
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.rect(BOARD_X, BOARD_Y, BOARD_W, BOARD_H, fill=0, stroke=1)

    # ── Scale verification bar ────────────────────────────────────────────────
    bar_y = BOARD_Y - 20*mm
    bar_x = BOARD_X

    c.setFont("Helvetica-Bold", 8)
    c.drawString(bar_x, bar_y + 8*mm,
                 "SCALE CHECK: measure this bar — must be exactly 100 mm.")

    # Solid black bar 100mm wide x 5mm tall
    c.setFillColor(colors.black)
    c.rect(bar_x, bar_y, 100*mm, 5*mm, fill=1, stroke=0)

    # Tick marks and label underneath
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.line(bar_x, bar_y - 2*mm, bar_x, bar_y + 5*mm + 2*mm)           # left tick
    c.line(bar_x + 100*mm, bar_y - 2*mm, bar_x + 100*mm, bar_y + 5*mm + 2*mm)  # right tick
    c.setFont("Helvetica", 8)
    c.drawCentredString(bar_x + 50*mm, bar_y - 6*mm, "<——— 100 mm ———>")

    # ── Print instructions ────────────────────────────────────────────────────
    instr_y = bar_y - 20*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(MARGIN, instr_y, "HOW TO PRINT AT CORRECT SIZE:")

    instructions = [
        "1. Open this PDF in Adobe Reader (NOT the browser — browser PDF viewers scale incorrectly).",
        "2. File -> Print -> under 'Page Sizing & Handling' select 'Actual Size'.",
        "3. Do NOT use 'Fit', 'Shrink', or 'Fit to Printable Area'.",
        "4. Print, then measure the black bar above. It must be exactly 100 mm.",
        "5. If not 100 mm, check printer driver scaling (Printer Properties -> no scaling).",
        "6. Once correct: laminate the sheet and place flat in the sorting dish.",
    ]
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate(instructions):
        c.drawString(MARGIN + 5*mm, instr_y - (i + 1) * 6*mm, line)

    # ── Notes ─────────────────────────────────────────────────────────────────
    note_y = instr_y - (len(instructions) + 2) * 6*mm
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawString(MARGIN, note_y,
                 "Note: OpenCV findChessboardCorners detects the 6x5 = 30 inner corners (black/white junctions). "
                 "The asymmetric board")
    c.drawString(MARGIN, note_y - 4.5*mm,
                 "(7 cols x 6 rows — not square in count) lets OpenCV determine orientation unambiguously.")

    c.save()
    board_w_mm = COLS * SQUARE_MM
    board_h_mm = ROWS * SQUARE_MM
    print(f"Saved: {OUT_FILE}")
    print(f"Board: {COLS}x{ROWS} squares x {SQUARE_MM}mm = {board_w_mm}mm x {board_h_mm}mm")
    print(f"OpenCV inner corners: {COLS-1}x{ROWS-1} = {(COLS-1)*(ROWS-1)} points")
    print("Print at ACTUAL SIZE from Adobe Reader.")


if __name__ == "__main__":
    build_pdf()
