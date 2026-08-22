"""
Real GigE Vision camera driver — Lucid TRI245S-CC via arena_api.

Identifies the camera by serial number (survives IP reassignment).
One instance per camera — overhead and EoA are separate instances.

Architecture: connect() opens the device, configures it, and starts
streaming (with retries). The stream stays alive for the entire session.
capture() just grabs a frame from the already-running stream — instant.
disconnect() stops the stream and destroys the device cleanly.

USB-Ethernet adapters (ASIX chipset) lack the Lucid LWF filter driver,
so start_stream's internal TestPacketSize handshake fails repeatedly.
It does eventually succeed after 6-8 retries (~20s). By keeping the
stream alive, this cost is paid once at startup — not per capture.

arena_api is only imported inside methods so dev laptops (no arena_api)
can still import this module without errors, as long as USE_MOCKS is True.
"""

from __future__ import annotations

import atexit
import asyncio
import ctypes
import threading
import time
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# Serializes all arena_api system-level operations across GigECameraDriver
# instances. arena_api's system object is a shared singleton — concurrent
# calls to discovery, create_device, or destroy_device race on the same
# GVCP UDP socket and produce GC_ERR_TIMEOUT or INVALID_INDEX (-1017).
_ARENA_SYSTEM_LOCK = threading.Lock()

# How long to wait for cameras to appear on network
CAMERA_DISCOVER_TIMEOUT_S = 15.0
# Warmup frames drained on the very first capture() call.
# In the pipeline, cameras have been streaming for ~14s before the first
# capture (connect → robot HOME → backlight → ring light → cycle start).
# By that point the sensor is long stabilised — warmup frames are not needed.
# The orchestrator already drains 2 stale frames before every keeper.
# Set to 0. (Hardware test capture_overhead.py uses 10 because it captures
# immediately after start_stream with no settle time — different use case.)
WARMUP_FRAMES = 0
# arena_api buffer timeout in milliseconds.
# USB-Ethernet adapters are slower than native NICs. 60s gives margin.
BUFFER_TIMEOUT_MS = 60_000
# How many times to retry start_stream before giving up.
# ASIX USB-Ethernet adapters need 6-8 attempts for TestPacketSize to
# eventually pass. 15 retries gives solid margin.
START_STREAM_RETRIES = 15
# Sleep between start_stream retries (seconds)
START_STREAM_RETRY_DELAY = 2.0

# ── atexit safety net ─────────────────────────────────────────────────
# If the process crashes or is Ctrl+C'd without clean shutdown,
# the arena_api system singleton still holds device references.
# The camera then refuses the next connection with GC_ERR_TIMEOUT
# because its control channel socket was never closed.
# This atexit handler calls system.destroy_device() with no arguments
# (= destroy ALL remaining devices) as a last-resort cleanup.
_atexit_registered = False


def _register_arena_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True

    def _cleanup_all_arena_devices() -> None:
        try:
            from arena_api.system import system
            system.destroy_device()          # None/no-arg = destroy ALL
        except Exception:
            pass
        # Also clear any unicast registrations
        try:
            from arena_api.system import system
            system.remove_unicast_discovery_device(None)  # None = remove all
        except Exception:
            pass

    atexit.register(_cleanup_all_arena_devices)


class GigECameraDriver:
    """
    Lucid TRI245S-CC GigE Vision camera driver.

    Args:
        serial:      Camera serial number string — e.g. "253102168" (overhead)
                     or "253102169" (EoA). Used to identify camera on network.
        target_ip:   IP to use for unicast discovery (e.g. "172.16.9.21").
        exposure_us: Fixed exposure time in microseconds.
        gain_db:     Analog gain in dB (0.0 = no gain).
        pixel_format: "Mono8" for grayscale (overhead), "BGR8" for color (EoA).
        max_fps:     Cap the camera acquisition frame rate (frames/second).
                     Both cameras stream continuously on the same USB-C Ethernet
                     adapter. Without a rate cap the combined bandwidth saturates
                     the adapter (~70MB/frame EoA + ~24MB/frame overhead), making
                     each get_buffer() call block for 1-2 seconds instead of
                     returning instantly. Set to None to leave at camera default.
        name:        Human label used in log messages.
    """

    def __init__(
        self,
        serial: str,
        target_ip: str = "",
        exposure_us: float = 2000.0,
        gain_db: float = 0.0,
        pixel_format: str = "Mono8",
        max_fps: float | None = None,
        name: str = "camera",
    ) -> None:
        self._serial = str(serial)
        self._target_ip = target_ip
        self._exposure_us = exposure_us
        self._gain_db = gain_db
        self._pixel_format = pixel_format
        self._max_fps = max_fps
        self._name = name
        self._device: Any = None
        self._streaming = False
        self._frame_count = 0
        self._abort_connect = threading.Event()

    # ------------------------------------------------------------------
    # connect — open device, configure, start stream (with retries)
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        from arena_api.system import system

        _register_arena_atexit()
        self._abort_connect.clear()

        if self._device is not None:
            log.warning("camera.stale_connection_cleanup", name=self._name)
            self._disconnect_sync()

        log.info("camera.connecting", name=self._name, serial=self._serial)

        with _ARENA_SYSTEM_LOCK:
            if self._target_ip:
                system.add_unicast_discovery_device(self._target_ip)

            infos = self._wait_for_cameras(system, timeout_s=CAMERA_DISCOVER_TIMEOUT_S,
                                           target_serial=self._serial)
            if self._abort_connect.is_set():
                log.info("camera.connect_aborted", name=self._name)
                return
            if not infos:
                raise RuntimeError(
                    f"No cameras found on network after {CAMERA_DISCOVER_TIMEOUT_S}s "
                    f"(camera={self._name}). Check PoE switch and camera power."
                )

            # Find our camera by serial number
            target_info = None
            found_serials = []
            seen_serials = set()
            for info in infos:
                sn = str(info.get("serial", ""))
                if sn in seen_serials:
                    continue
                seen_serials.add(sn)
                found_serials.append(sn)
                if sn == self._serial:
                    target_info = info

            if target_info is None:
                raise RuntimeError(
                    f"Camera serial {self._serial} not found on network. "
                    f"Found serials: {found_serials}. "
                    f"Check cable and PoE switch power."
                )

            current_ip = str(target_info.get("ip", ""))
            log.debug("camera.found", name=self._name,
                      serial=self._serial, current_ip=current_ip)

            # If camera is not on the target IP (e.g. DHCP/LLA gave it
            # 169.254.x.x), force_ip it to the target address.
            if self._target_ip and current_ip != self._target_ip:
                log.info("camera.force_ip", name=self._name,
                         from_ip=current_ip, to_ip=self._target_ip)
                self._force_ip(system, target_info, self._target_ip,
                               subnet="255.255.255.0")
                time.sleep(3.0)
                # Re-discover after force_ip
                infos = self._wait_for_cameras(system, timeout_s=10.0,
                                               target_serial=self._serial)
                for info in infos:
                    if str(info.get("serial", "")) == self._serial:
                        target_info = info
                        break
                log.debug("camera.after_force_ip", name=self._name,
                          ip=str(target_info.get("ip", "")))

            # Open the device, retrying on transient errors.
            devices = None
            first_err = None
            for _attempt in range(6):
                if self._abort_connect.is_set():
                    log.info("camera.connect_aborted", name=self._name)
                    return
                try:
                    devices = system.create_device(device_infos=target_info)
                    first_err = None
                    break
                except Exception as exc:
                    err_str = str(exc)
                    first_err = exc
                    if ("INVALID_INDEX" in err_str or "-1017" in err_str or
                            "GC_ERR_TIMEOUT" in err_str or "-1011" in err_str):
                        log.debug("camera.create_device_retry", name=self._name,
                                  attempt=_attempt + 1, error=err_str[:80])
                        time.sleep(2.0)
                        infos = self._wait_for_cameras(system, timeout_s=5.0,
                                                       target_serial=self._serial)
                        for info in infos:
                            if str(info.get("serial", "")) == self._serial:
                                target_info = info
                                break
                        continue
                    break

        if first_err is not None:
            raise RuntimeError(
                f"Camera {self._serial} ({self._name}) could not be opened: {first_err}"
            ) from first_err
        if not devices or len(devices) == 0:
            raise RuntimeError(
                f"create_device() returned empty list for serial {self._serial}"
            )
        self._device = devices[0]

        # Configure camera settings
        self._configure_device()

        # Start stream — retries needed on ASIX USB-Ethernet adapters.
        # This is the one-time cost; after this, captures are instant.
        self._start_stream_with_retry()

        log.info("camera.connected", name=self._name, serial=self._serial,
                 ip=target_info.get("ip"), exposure_us=self._exposure_us,
                 pixel_format=self._pixel_format)

    def _configure_device(self) -> None:
        """Set exposure, gain, pixel format, and GigE packet tuning."""
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

        # ── White balance (color cameras only) ───────────────────────────
        # Arena View has BalanceWhiteAuto=Continuous on by default.
        # Without this, warm ring-light color temperature bleeds through as
        # a yellow cast whenever the backlight doesn't fully saturate the
        # background (e.g. at 5000µs). Mono8 cameras don't have WB nodes.
        if self._pixel_format != "Mono8":
            try:
                nm["BalanceWhiteAuto"].value = "Continuous"
                log.debug("camera.white_balance_auto_enabled", name=self._name)
            except Exception as e:
                log.warning("camera.white_balance_auto_failed", name=self._name,
                            error=str(e)[:80])

        # ── Frame rate cap ───────────────────────────────────────────────
        # Both cameras share a single USB-C Ethernet adapter. Without a
        # frame rate cap, combined bandwidth (overhead 24MB + EoA 70MB per
        # frame at max rate) saturates the adapter, causing get_buffer() to
        # block for 1-2s per frame and making 10 warmup frames take ~20s.
        if self._max_fps is not None:
            try:
                nm["AcquisitionFrameRateEnable"].value = True
                nm["AcquisitionFrameRate"].value = float(self._max_fps)
                log.debug("camera.frame_rate_set", name=self._name, fps=self._max_fps)
            except Exception as e:
                log.warning("camera.frame_rate_set_failed", name=self._name,
                            error=str(e)[:80])

        # ── GigE tuning for USB-Ethernet adapters ────────────────────────
        # Packet size: 1400B fits within USB-Ethernet MTU (no jumbo frames).
        try:
            nm["GevSCPSPacketSize"].value = 1400
        except Exception:
            pass

        tl_stream = self._device.tl_stream_nodemap

        # Disable auto-negotiate — avoids TestPacketSize timeout on USB adapters.
        try:
            tl_stream["StreamAutoNegotiatePacketSize"].value = False
        except Exception as e:
            log.debug("camera.auto_negotiate_disable_failed", name=self._name,
                      error=str(e)[:80])

        # Enable packet resend — when the USB adapter drops a GigE packet the
        # SDK sends a NACK and the camera retransmits the missing packet.
        # Without this, a dropped packet leaves a black scan line in the image.
        # Node confirmed present on TRI245S-CC via stream_nodes_test.py output.
        try:
            tl_stream["StreamPacketResendEnable"].value = True
            log.debug("camera.packet_resend_enabled", name=self._name)
        except Exception as e:
            log.warning("camera.packet_resend_enable_failed", name=self._name,
                        error=str(e)[:80])

    def _start_stream_with_retry(self) -> None:
        """Start streaming with retries. Cleans up partial state between attempts.

        Checks _abort_connect between retries so shutdown doesn't block.
        """
        last_err: Exception | None = None
        for attempt in range(START_STREAM_RETRIES):
            if self._abort_connect.is_set():
                raise RuntimeError(f"Camera '{self._name}' connect aborted (shutdown)")
            if not self._device:
                raise RuntimeError(f"Camera '{self._name}' device was destroyed")
            try:
                self._device.start_stream(2)
                self._streaming = True
                if attempt > 0:
                    log.info("camera.start_stream_ok", name=self._name,
                             attempt=attempt + 1)
                return
            except Exception as exc:
                last_err = exc
                err_str = str(exc)
                retryable = (
                    "GC_ERR_TIMEOUT" in err_str or "-1011" in err_str or
                    "GC_ERR_BUSY"    in err_str or "-1022" in err_str
                )
                if retryable and attempt < START_STREAM_RETRIES - 1:
                    log.debug("camera.start_stream_retry", name=self._name,
                              attempt=attempt + 1, error=err_str[:80])
                    try:
                        self._device.stop_stream()
                    except Exception:
                        pass
                    time.sleep(START_STREAM_RETRY_DELAY)
                    continue
                raise

        raise RuntimeError(
            f"Camera {self._serial} ({self._name}) start_stream failed "
            f"after {START_STREAM_RETRIES} attempts: {last_err}"
        ) from last_err

    # ------------------------------------------------------------------
    # start_streaming / stop_streaming — called by orchestrator
    # ------------------------------------------------------------------

    async def start_streaming(self) -> None:
        """Start frame acquisition. No-op if already streaming."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._start_streaming_sync)

    def _start_streaming_sync(self) -> None:
        if self._streaming:
            return
        if not self._device:
            raise RuntimeError(f"Camera '{self._name}' not connected — call connect() first")
        self._start_stream_with_retry()
        log.info("camera.streaming_started", name=self._name)

    async def stop_streaming(self) -> None:
        """Stop frame acquisition. No-op if already stopped."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._stop_streaming_sync)

    def _stop_streaming_sync(self) -> None:
        if not self._streaming or not self._device:
            return
        try:
            self._device.stop_stream()
        except Exception as exc:
            log.warning("camera.stop_stream_error", name=self._name, error=str(exc))
        self._streaming = False
        log.info("camera.streaming_stopped", name=self._name)

    # ------------------------------------------------------------------
    # disconnect
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        from arena_api.system import system

        self._abort_connect.set()

        # 1. Stop the stream explicitly.
        #    destroy_device() will also close it internally, but stopping
        #    first ensures all buffers are properly requeued.
        if self._device and self._streaming:
            try:
                self._device.stop_stream()
            except Exception:
                pass
            self._streaming = False

        # 2. Destroy the device — closes control channel socket so the
        #    camera can be opened in read-write mode on the next run.
        #    Per SDK docs: "the control channel socket is closed, allowing
        #    the device to be opened in read-write mode again."
        if self._device:
            try:
                system.destroy_device(self._device)
            except Exception:
                pass
            self._device = None

        # 3. Remove unicast discovery registration that was added in
        #    connect(). Per SDK docs: "The list of remote devices will
        #    persist until they are removed using
        #    remove_unicast_discovery_device() or until the application
        #    terminates."
        if self._target_ip:
            try:
                system.remove_unicast_discovery_device(self._target_ip)
            except Exception:
                pass

        # 4. Let the camera's GigE control channel fully release.
        #    0.5s is often too short for USB-Ethernet adapters; 1.0s is safer.
        time.sleep(1.0)

        log.info("camera.disconnected", name=self._name)

    # ------------------------------------------------------------------
    # capture — grab frame from already-running stream (instant)
    # ------------------------------------------------------------------

    async def capture(self) -> np.ndarray:
        """
        Capture one frame from the already-running stream.
        Returns HxW uint8 (Mono8) or HxWx3 uint8 (BGR8).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._capture_sync)

    async def set_exposure(self, exposure_us: float) -> None:
        """
        Change exposure time in µs while the stream is running.
        The new exposure takes effect on the next frame the sensor integrates —
        drain at least one frame after calling this before taking the keeper.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_exposure_sync, exposure_us)

    def _set_exposure_sync(self, exposure_us: float) -> None:
        if not self._device:
            raise RuntimeError(f"Camera '{self._name}' not connected — call connect() first")
        self._device.nodemap["ExposureTime"].value = float(exposure_us)
        self._exposure_us = exposure_us
        log.debug("camera.exposure_set", name=self._name, exposure_us=exposure_us)

    async def lock_white_balance(self) -> bool:
        """
        Converge white balance using BalanceWhiteAuto="Once" — the
        GenICam-standard self-terminating auto function: the camera runs
        its own convergence algorithm against captured frames and resets
        BalanceWhiteAuto to "Off" itself once satisfied (or after an
        internal max-iteration safety cap), then holds those gains fixed
        for every subsequent frame.

        REWRITTEN 2026-08-21 — the original version ran Continuous for a
        guessed number of frames, then forcibly flipped it to Off. Real
        hardware testing showed that can catch a not-yet-converged
        transient state and freeze it forever, producing badly overexposed
        captures with no way to self-correct. This version never guesses:
        it polls the node and only treats it as done when the camera
        itself reports Off. Must be called while nothing else is
        capturing (this method grabs and requeues its own frames to drive
        convergence).

        Returns True if BalanceWhiteAuto confirmed Off within
        MAX_CONVERGE_FRAMES, False if it never did — on False, this
        restores BalanceWhiteAuto to Continuous as a safe fallback (the
        previously-proven-working behavior) rather than leaving it in an
        uncertain "Once, maybe still running" state. Callers should only
        treat True as "safe to skip re-converging on future calls."
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._lock_white_balance_sync)

    def _lock_white_balance_sync(self) -> bool:
        if not self._device:
            raise RuntimeError(f"Camera '{self._name}' not connected — call connect() first")
        if self._pixel_format == "Mono8":
            return True   # no WB nodes on a mono sensor — nothing to converge

        MAX_CONVERGE_FRAMES = 15

        try:
            self._device.nodemap["BalanceWhiteAuto"].value = "Once"
        except Exception as e:
            log.warning("camera.white_balance_once_trigger_failed", name=self._name,
                        error=str(e)[:80])
            return False

        for i in range(MAX_CONVERGE_FRAMES):
            try:
                buf = self._device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
                self._device.requeue_buffer(buf)
            except Exception as e:
                log.warning("camera.white_balance_converge_frame_failed", name=self._name,
                            error=str(e)[:80])
                break
            try:
                current = self._device.nodemap["BalanceWhiteAuto"].value
            except Exception:
                current = None
            if current == "Off":
                log.info("camera.white_balance_converged", name=self._name, frames=i + 1)
                return True

        # Never confirmed Off — restore Continuous rather than leave it
        # uncertain. Same behavior as before this method ever existed.
        try:
            self._device.nodemap["BalanceWhiteAuto"].value = "Continuous"
        except Exception:
            pass
        log.warning("camera.white_balance_converge_timeout", name=self._name,
                    frames=MAX_CONVERGE_FRAMES,
                    note="BalanceWhiteAuto never self-reported Off — restored to "
                         "Continuous, will retry convergence next time this is called")
        return False

    def _capture_sync(self) -> np.ndarray:
        if not self._device or not self._streaming:
            raise RuntimeError(f"Camera '{self._name}' is not connected or not streaming")

        # Drain warmup frames on first capture so exposure settles.
        # 10 frames matches capture_overhead.py (hardware test) which produces
        # clean images — fewer frames risk reading before the sensor stabilises.
        if self._frame_count == 0:
            for _ in range(10):
                buf = self._device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
                self._device.requeue_buffer(buf)

        buf = self._device.get_buffer(timeout=BUFFER_TIMEOUT_MS)
        try:
            bpp    = int(buf.bits_per_pixel) // 8
            height = int(buf.height)
            width  = int(buf.width)
            total  = height * width * bpp

            raw = (ctypes.c_uint8 * total).from_address(ctypes.addressof(buf.pbytes))
            flat = np.ctypeslib.as_array(raw).copy()

            frame = flat.reshape(height, width) if bpp == 1 else flat.reshape(height, width, bpp)
        finally:
            self._device.requeue_buffer(buf)

        self._frame_count += 1
        log.debug("camera.captured", name=self._name, frame=self._frame_count,
                  shape=frame.shape)
        return frame

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _force_ip(system: Any, device_info: dict, target_ip: str,
                  subnet: str = "255.255.0.0", gateway: str = "0.0.0.0") -> None:
        forced = dict(device_info)
        forced["ip"]             = target_ip
        forced["subnetmask"]     = subnet
        forced["defaultgateway"] = gateway
        system.force_ip(forced)

    def _wait_for_cameras(self, system: Any, timeout_s: float,
                          target_serial: str = "") -> list:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._abort_connect.is_set():
                return []
            infos = list(system.device_infos)
            if infos:
                if not target_serial:
                    return infos
                for info in infos:
                    if str(info.get("serial", "")) == target_serial:
                        return infos
            time.sleep(0.5)
        return list(system.device_infos)
