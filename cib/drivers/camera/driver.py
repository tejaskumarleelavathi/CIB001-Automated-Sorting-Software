"""
Real GigE Vision camera driver — Lucid TRI245S-CC via arena_api.

Identifies the camera by serial number (survives IP reassignment).
One instance per camera — overhead and EoA are separate instances.

arena_api is only imported inside methods so dev laptops (no arena_api)
can still import this module without errors, as long as USE_MOCKS is True.
"""

from __future__ import annotations

import asyncio
import ctypes
import time
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# How long to wait for cameras to appear on network
CAMERA_DISCOVER_TIMEOUT_S = 10.0
# Drain this many frames before the keeper frame so exposure settles.
# capture.py (hardware test) uses 10 — match that.
WARMUP_FRAMES = 10
# arena_api buffer timeout in milliseconds
BUFFER_TIMEOUT_MS = 5000


class GigECameraDriver:
    """
    Lucid TRI245S-CC GigE Vision camera driver.

    Args:
        serial:      Camera serial number string — e.g. "253102168" (overhead)
                     or "253102169" (EoA). Used to identify camera on network.
        exposure_us: Fixed exposure time in microseconds.
        gain_db:     Analog gain in dB (0.0 = no gain).
        pixel_format: "Mono8" for grayscale (overhead), "BGR8" for color (EoA).
        name:        Human label used in log messages.
    """

    def __init__(
        self,
        serial: str,
        target_ip: str = "",
        exposure_us: float = 2000.0,
        gain_db: float = 0.0,
        pixel_format: str = "Mono8",
        name: str = "camera",
    ) -> None:
        self._serial = str(serial)
        self._target_ip = target_ip          # IP to force-assign (e.g. "172.16.9.21")
        self._exposure_us = exposure_us
        self._gain_db = gain_db
        self._pixel_format = pixel_format
        self._name = name
        self._device: Any = None
        self._streaming = False
        self._frame_count = 0

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        from arena_api.system import system

        log.info("camera.connecting", name=self._name, serial=self._serial)

        # Wait for cameras to appear on the network
        infos = self._wait_for_cameras(system, timeout_s=CAMERA_DISCOVER_TIMEOUT_S)
        if not infos:
            raise RuntimeError(
                f"No cameras found on network after {CAMERA_DISCOVER_TIMEOUT_S}s "
                f"(camera={self._name}). Check PoE switch and camera power."
            )

        # Find our camera by serial number
        target_info = None
        found_serials = []
        for info in infos:
            sn = str(info.get("serial", ""))
            found_serials.append(sn)
            if sn == self._serial:
                target_info = info
                break

        if target_info is None:
            raise RuntimeError(
                f"Camera serial {self._serial} not found on network. "
                f"Found serials: {found_serials}. "
                f"Check cable and run force_camera_ip.py if this is the first session."
            )

        current_ip = str(target_info.get("ip", ""))
        log.debug("camera.found", name=self._name,
                  serial=self._serial, current_ip=current_ip)

        # Try to open the device as-is first (works if camera has a persistent
        # static IP already configured in its NVM via ArenaView GUI).
        # If it fails with a subnet/address error, fall back to force_ip.
        try:
            devices = system.create_device(device_infos=target_info)
        except Exception as first_err:
            err_str = str(first_err)
            if "different subnet" not in err_str and "INVALID_ADDRESS" not in err_str:
                raise  # unrelated error — don't swallow it

            # Camera is on wrong subnet (likely link-local 169.254.x.x default).
            # force_ip sends a Layer-2 GVC command to the camera's MAC address
            # to assign it the target IP, then we reconnect.
            # PERMANENT FIX: configure a static IP in ArenaView GUI so this never runs.
            if not self._target_ip:
                raise RuntimeError(
                    f"Camera {self._serial} is on a different subnet ({current_ip}) "
                    f"and no target_ip is configured to force-assign. "
                    f"Set a persistent static IP in ArenaView GUI."
                ) from first_err

            log.warning("camera.wrong_subnet_forcing_ip", name=self._name,
                        current_ip=current_ip, target_ip=self._target_ip,
                        note="Set persistent static IP in ArenaView to avoid this delay")

            self._force_ip(system, target_info, self._target_ip)
            time.sleep(1.5)   # camera reboots its network stack

            # Rediscover with the new IP
            infos = self._wait_for_cameras(system, timeout_s=8.0)
            target_info = None
            for info in infos:
                if str(info.get("serial", "")) == self._serial:
                    target_info = info
                    break
            if target_info is None:
                raise RuntimeError(
                    f"Camera serial {self._serial} did not reappear after force_ip "
                    f"to {self._target_ip}. Check network connectivity."
                )

            log.info("camera.force_ip_done", name=self._name,
                     new_ip=target_info.get("ip"))
            devices = system.create_device(device_infos=target_info)
        if not devices:
            raise RuntimeError(
                f"create_device() returned empty list for serial {self._serial}"
            )
        self._device = devices[0]

        # Configure camera
        nm = self._device.nodemap
        try:
            nm["PixelFormat"].value = self._pixel_format
        except Exception as e:
            log.warning("camera.pixel_format_failed", name=self._name, error=str(e),
                        current=nm["PixelFormat"].value)

        nm["ExposureAuto"].value = "Off"
        nm["ExposureTime"].value = float(self._exposure_us)
        nm["GainAuto"].value = "Off"
        nm["Gain"].value = float(self._gain_db)

        # Start stream — 2 buffers minimum per arena_api requirement
        self._device.start_stream(2)
        self._streaming = True

        log.info("camera.connected", name=self._name, serial=self._serial,
                 ip=target_info.get("ip"), exposure_us=self._exposure_us,
                 pixel_format=self._pixel_format)

    @staticmethod
    def _force_ip(system: Any, device_info: dict, target_ip: str,
                  subnet: str = "255.255.255.0", gateway: str = "") -> None:
        """
        Force-assign target_ip to the camera described by device_info.
        Passes a modified device_info dict to system.force_ip() — that is the
        correct arena_api signature (one dict argument, not serial + integers).
        """
        if not gateway:
            parts = target_ip.rsplit(".", 1)
            gateway = parts[0] + ".1"

        forced = dict(device_info)
        forced["ip"]             = target_ip
        forced["subnetmask"]     = subnet
        forced["defaultgateway"] = gateway

        system.force_ip(forced)

    @staticmethod
    def _wait_for_cameras(system: Any, timeout_s: float) -> list:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            infos = list(system.device_infos)
            if infos:
                return infos
            time.sleep(0.5)
        return list(system.device_infos)

    async def disconnect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        if self._device and self._streaming:
            try:
                self._device.stop_stream()
            except Exception:
                pass
            self._streaming = False
        if self._device:
            try:
                from arena_api.system import system
                system.destroy_device(self._device)
            except Exception:
                pass
            self._device = None
        log.info("camera.disconnected", name=self._name)

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    async def capture(self) -> np.ndarray:
        """
        Capture one frame. Returns HxW uint8 numpy array (Mono8) or
        HxWx3 uint8 array (BGR8). Runs in thread — arena_api is blocking.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._capture_sync)

    def _capture_sync(self) -> np.ndarray:
        if not self._device or not self._streaming:
            raise RuntimeError(f"Camera '{self._name}' is not connected or not streaming")

        # Drain warmup frames on first capture so exposure settles
        if self._frame_count == 0:
            for _ in range(WARMUP_FRAMES):
                buf = self._device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
                self._device.requeue_buffer(buf)

        buf = self._device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
        try:
            bpp    = int(buf.bits_per_pixel) // 8
            height = int(buf.height)
            width  = int(buf.width)
            total  = height * width * bpp

            # Use pbytes + ctypes.addressof() — NOT buf.pdata (causes integer error)
            raw = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
            flat = np.ctypeslib.as_array(raw).copy()   # .copy() detaches from buffer

            frame = flat.reshape(height, width) if bpp == 1 else flat.reshape(height, width, bpp)
        finally:
            self._device.requeue_buffer(buf)

        self._frame_count += 1
        log.debug("camera.captured", name=self._name, frame=self._frame_count,
                  shape=frame.shape)
        return frame
