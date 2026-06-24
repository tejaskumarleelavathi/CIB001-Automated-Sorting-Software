"""
Mock camera driver — returns a synthetic test image.

In real use, swap this for the GigE Vision harvesters driver.
The mock generates a numpy array with fake bright spots so the
vision pipeline has something real to process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# If you have real test images, put them in tests/assets/ and load them here
TEST_IMAGE_PATH = Path(__file__).parent.parent.parent.parent / "tests" / "assets" / "sample_overhead.png"


class MockCameraDriver:

    def __init__(self, name: str = "mock_camera", num_fake_samples: int = 5) -> None:
        self._name = name
        self._num_fake_samples = num_fake_samples
        self._connected = False
        self._frame_count = 0

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        self._connected = True
        log.info("mock_camera.connected", name=self._name)

    async def disconnect(self) -> None:
        self._connected = False

    async def capture(self) -> Any:
        """Returns a 1280x1024 grayscale numpy array with synthetic sample spots."""
        await asyncio.sleep(0.05)   # simulate exposure time
        self._frame_count += 1

        if TEST_IMAGE_PATH.exists():
            import cv2
            frame = cv2.imread(str(TEST_IMAGE_PATH), cv2.IMREAD_GRAYSCALE)
            log.info("mock_camera.captured_from_file", name=self._name, frame=self._frame_count)
            return frame

        # Generate synthetic frame: dark background, bright spots = samples
        frame = np.zeros((1024, 1280), dtype=np.uint8)
        rng = np.random.default_rng(seed=self._frame_count)
        for _ in range(self._num_fake_samples):
            cx = int(rng.integers(100, 1180))
            cy = int(rng.integers(100, 924))
            cv2_circle_mock(frame, cx, cy, radius=15, value=220)

        log.info("mock_camera.captured_synthetic", name=self._name,
                 frame=self._frame_count, spots=self._num_fake_samples)
        return frame


def cv2_circle_mock(frame: np.ndarray, cx: int, cy: int, radius: int, value: int) -> None:
    """Draw a filled circle — avoids importing cv2 just for the mock."""
    y, x = np.ogrid[:frame.shape[0], :frame.shape[1]]
    mask = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
    frame[mask] = value
