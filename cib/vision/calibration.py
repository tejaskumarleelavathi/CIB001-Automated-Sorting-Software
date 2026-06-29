"""
Camera calibration — production library.

Used by the orchestrator and vision pipeline at runtime.
The calibration scripts in camera_calibration/ write the .npy file;
this module loads it and exposes the pixel→robot transform.
"""

from __future__ import annotations

import numpy as np
import structlog

log = structlog.get_logger(__name__)

DEFAULT_H_PATH      = r"C:\LabAutomation\config\calibration_overhead.npy"
EOA_CALIBRATION_PATH = r"C:\LabAutomation\config\calibration_eoa.npy"


class CalibrationNotFoundError(RuntimeError):
    """Raised when no calibration file exists on disk."""


class CalibrationData:
    """
    Holds the overhead camera homography matrix H.
    Load once at startup, pass around as needed.
    """

    def __init__(self, H: np.ndarray) -> None:
        if H.shape != (3, 3):
            raise ValueError(f"Homography must be 3x3, got {H.shape}")
        self._H = H

    def pixel_to_robot(self, px: float, py: float) -> tuple[float, float]:
        """
        Convert overhead camera pixel (px, py) → robot world XY (mm).

        Args:
            px: pixel X coordinate (0 = left edge of image)
            py: pixel Y coordinate (0 = top edge of image)

        Returns:
            (robot_x_mm, robot_y_mm) in Epson T3-B world coordinates
        """
        import cv2
        pt = np.array([[[px, py]]], dtype=np.float32)
        result = cv2.perspectiveTransform(pt, self._H)
        rx = float(result[0][0][0])
        ry = float(result[0][0][1])
        log.debug("calibration.pixel_to_robot", px=px, py=py, rx=rx, ry=ry)
        return rx, ry

    def pixel_list_to_robot(
        self, centroids: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """Convert a list of pixel centroids to robot XY coordinates."""
        return [self.pixel_to_robot(px, py) for px, py in centroids]

    @property
    def H(self) -> np.ndarray:
        return self._H.copy()


def load_calibration(path: str = DEFAULT_H_PATH) -> CalibrationData:
    """
    Load the overhead homography matrix from disk.

    Raises CalibrationNotFoundError if no calibration file exists.
    Call once at startup and store the result.
    """
    import os
    if not os.path.exists(path):
        raise CalibrationNotFoundError(
            f"No calibration file found at: {path}\n"
            "Run camera_calibration/step1_detect_corners.py then "
            "camera_calibration/step2_compute_homography.py first."
        )
    H = np.load(path)
    log.info("calibration.loaded", path=path, shape=H.shape)
    return CalibrationData(H)
