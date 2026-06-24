"""
cib/vision/eoa.py — EoA (End-of-Arm) camera vision analysis.

TODO: implement go/no-go decision and refined centroid extraction from EoA image.

Stub returns proceed=True with no centroid refinement so the pick-place cycle
can run end-to-end during development.
"""

from __future__ import annotations

from typing import Any

import structlog

from cib.core.events import VisionEoAResultEvent

log = structlog.get_logger(__name__)


def run_eoa_analysis(frame: Any) -> VisionEoAResultEvent:
    """
    Stub — always returns go (proceed_with_pick=True) with no centroid refinement.
    Replace with real implementation when EoA vision is ready.
    """
    log.debug("eoa.stub", note="EoA vision not yet implemented — returning go")
    return VisionEoAResultEvent(
        proceed_with_pick=True,
        refined_x=None,
        refined_y=None,
        frame_path="",
    )
