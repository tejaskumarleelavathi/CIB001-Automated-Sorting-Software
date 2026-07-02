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


# ---------------------------------------------------------------------------
# EoA camera calibration  (different maths from overhead homography)
# ---------------------------------------------------------------------------

class EoACalibrationData:
    """
    Holds the EoA camera affine calibration produced by
    camera_calibration/eoa_calibrate.py.

    The calibration file is a (2, 3) numpy array:
        cal[:, :2]  — 2×2 matrix M_inv  (pixel offset → robot-mm delta)
        cal[:, 2]   — 2-element cam_offset (fixed camera-centre-to-tool offset, mm)

    To convert a body-centre pixel (bx, by) in an image of size (W, H)
    to an absolute robot pick position given the current robot position
    (robot_x, robot_y):

        pixel_offset = [bx - W/2,  by - H/2]          # relative to image centre
        robot_delta  = M_inv @ pixel_offset + cam_offset
        pick_x       = robot_x + robot_delta[0]
        pick_y       = robot_y + robot_delta[1]

    This is exposed via pixel_to_absolute_robot().
    """

    def __init__(self, cal: np.ndarray) -> None:
        if cal.shape != (2, 3):
            raise ValueError(f"EoA calibration must be (2,3), got {cal.shape}")
        self._M_inv     = cal[:, :2]          # (2,2) pixel-delta → robot-mm delta
        self._cam_offset = cal[:, 2]          # (2,) fixed camera-to-tool offset

    def pixel_to_absolute_robot(
        self,
        body_cx: float, body_cy: float,
        image_w: int, image_h: int,
        robot_x: float, robot_y: float,
    ) -> tuple[float, float]:
        """
        Convert EoA body-centre pixel to absolute robot pick XY (mm).

        Args:
            body_cx/cy: body-centre pixel coordinates in EoA image
            image_w/h:  EoA image dimensions in pixels
            robot_x/y:  current robot position (mm) — the overhead centroid
                        the arm was sent to before EoA imaging

        Returns:
            (pick_x_mm, pick_y_mm) in robot world coordinates
        """
        dx_px = body_cx - image_w / 2.0
        dy_px = body_cy - image_h / 2.0
        pixel_offset = np.array([dx_px, dy_px], dtype=float)
        robot_delta  = self._M_inv @ pixel_offset + self._cam_offset
        pick_x = robot_x + float(robot_delta[0])
        pick_y = robot_y + float(robot_delta[1])
        log.debug(
            "eoa_calibration.pixel_to_robot",
            body_px=f"({body_cx:.1f}, {body_cy:.1f})",
            pixel_offset=f"({dx_px:.1f}, {dy_px:.1f})",
            robot_delta_mm=f"({robot_delta[0]:.3f}, {robot_delta[1]:.3f})",
            pick_xy_mm=f"({pick_x:.3f}, {pick_y:.3f})",
        )
        return pick_x, pick_y


def load_eoa_calibration(path: str = EOA_CALIBRATION_PATH) -> EoACalibrationData:
    """
    Load the EoA affine calibration from disk.

    Raises CalibrationNotFoundError if no calibration file exists.
    Run camera_calibration/eoa_calibrate.py to create the file.
    """
    import os
    if not os.path.exists(path):
        raise CalibrationNotFoundError(
            f"No EoA calibration file found at: {path}\n"
            "Run camera_calibration/eoa_calibrate.py first."
        )
    cal = np.load(path)
    log.info("eoa_calibration.loaded", path=path, shape=cal.shape)
    return EoACalibrationData(cal)
