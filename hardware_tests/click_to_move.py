#!/usr/bin/env python3
"""
hardware_tests/click_to_move.py  —  Click-to-move calibration verifier
=======================================================================

Flow:
  1. Startup exactly mirrors main.py (cameras → lighting → robot HOME → backlight)
  2. One overhead snapshot captured (same drain-2-keep-1 as the main pipeline)
  3. Snapshot shown as static image — click any bug
  4. Robot moves EoA camera above that pixel position using the calibration
  5. EoA image captured and shown in second window
  6. Overhead snapshot refreshed for the next click

If the EoA shows what you clicked -> calibration is correct.
If it is offset -> homography needs to be redone.

Usage:
    python hardware_tests/click_to_move.py

Controls (click the Overhead window):
    Left-click  ->  move robot there, capture EoA
    R           ->  refresh overhead snapshot
    H           ->  home robot
    Q / ESC     ->  home robot and quit
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

DISPLAY_SCALE = 0.24   # 5320x4600 -> ~1277x1104 display window

# ── Inter-thread state ────────────────────────────────────────────────────────
_frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
_eoa_q:   "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
_click_q: "queue.Queue"             = queue.Queue()
_done     = threading.Event()
_status   = {"text": "Connecting...", "colour": (0, 165, 255)}


def _put(q, item) -> None:
    while not q.empty():
        try: q.get_nowait()
        except queue.Empty: break
    q.put(item)


def _to_display(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = frame.shape[:2]
    return cv2.resize(frame, (int(w * DISPLAY_SCALE), int(h * DISPLAY_SCALE)),
                      interpolation=cv2.INTER_AREA)


def _annotate(img: np.ndarray, text: str, colour: tuple) -> np.ndarray:
    out = img.copy()
    font  = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.55, img.shape[1] / 2200)
    thick = max(1, int(scale * 2))
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    pad = 10
    cv2.rectangle(out, (pad, pad), (pad * 2 + tw, pad * 2 + th), (0, 0, 0), -1)
    cv2.putText(out, text, (int(pad * 1.5), pad + th), font, scale, colour, thick)
    return out


# ── Display — main thread ─────────────────────────────────────────────────────

def _display_main() -> None:
    WIN_OH  = "Overhead  (click to move robot)"
    WIN_EOA = "EoA  (robot arrived here)"
    cv2.namedWindow(WIN_OH,  cv2.WINDOW_AUTOSIZE)
    cv2.namedWindow(WIN_EOA, cv2.WINDOW_AUTOSIZE)

    dh, dw = int(4600 * DISPLAY_SCALE), int(5320 * DISPLAY_SCALE)
    oh  = np.zeros((dh, dw, 3), dtype=np.uint8)
    eoa = np.zeros((dh, dw, 3), dtype=np.uint8)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            _click_q.put(("click", x / DISPLAY_SCALE, y / DISPLAY_SCALE))

    cv2.setMouseCallback(WIN_OH, on_mouse)
    hint = "L-click: move robot  |  R: refresh overhead  |  H: home  |  Q: quit"

    while not _done.is_set():
        try: oh  = _frame_q.get_nowait()
        except queue.Empty: pass
        try: eoa = _eoa_q.get_nowait()
        except queue.Empty: pass

        disp = _annotate(oh, _status["text"], _status["colour"])
        cv2.putText(disp, hint, (10, disp.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 160, 160), 1)
        cv2.imshow(WIN_OH,  disp)
        cv2.imshow(WIN_EOA, eoa)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), 27):
            _click_q.put(("quit",))
            _done.set()
        elif key in (ord("h"), ord("H")):
            _click_q.put(("home",))
        elif key in (ord("r"), ord("R")):
            _click_q.put(("refresh",))

    cv2.destroyAllWindows()


# ── Async — background thread ─────────────────────────────────────────────────

async def _capture_overhead_snapshot(overhead) -> np.ndarray:
    """
    Capture one overhead frame exactly like main.py _run_cycle().
    start_streaming → drain 2 stale → keep 1 → stop_streaming.
    """
    await overhead.start_streaming()
    await overhead.capture()   # drain stale 1
    await overhead.capture()   # drain stale 2
    frame = await overhead.capture()   # fresh stable frame
    await overhead.stop_streaming()
    return frame


async def _async_main() -> None:
    from cib.logging_setup import configure as configure_logging
    from cib.drivers.camera.driver   import GigECameraDriver
    from cib.drivers.robot.driver    import EpsonT3BDriver
    from cib.drivers.lighting.driver import LightingControllerDriver
    from cib.vision.calibration      import load_calibration
    from cib.config.loader           import load_machine_config

    configure_logging()

    _prod = Path(r"C:\LabAutomation\config\machine_config.yaml")
    _dev  = Path(__file__).parent.parent / "config" / "machine_config.yaml"
    cfg   = load_machine_config(_prod if _prod.exists() else _dev)

    oh_cfg  = cfg.overhead_camera
    eoa_cfg = cfg.eoa_camera

    overhead = GigECameraDriver(
        serial=oh_cfg.serial, target_ip=oh_cfg.ip,
        exposure_us=oh_cfg.exposure_us, gain_db=oh_cfg.gain_db,
        pixel_format=oh_cfg.pixel_format,
        max_fps=getattr(oh_cfg, "max_fps", None), name="overhead",
    )
    eoa_cam = GigECameraDriver(
        serial=eoa_cfg.serial, target_ip=eoa_cfg.ip,
        exposure_us=eoa_cfg.exposure_us, gain_db=eoa_cfg.gain_db,
        pixel_format=eoa_cfg.pixel_format,
        max_fps=getattr(eoa_cfg, "max_fps", None), name="eoa",
    )
    robot    = EpsonT3BDriver(ip=cfg.robot.ip, port=cfg.robot.port)
    lighting = LightingControllerDriver(
        ip=cfg.backlight.ip, port=cfg.backlight.port,
        current_ma=cfg.backlight.current_ma,
    )

    # ── Startup: exactly mirrors orchestrator._startup() ──────────────────────
    # Cameras connect first (GigE discovery + stream start takes ~20s on USB-Ethernet).
    # Robot connects AFTER so it is never left idle on TCP while cameras negotiate.
    # Overhead stays streaming during robot HOME so the sensor warms up (~14s).
    print("[startup] Connecting cameras...")
    await asyncio.gather(overhead.connect(), eoa_cam.connect())
    # Stop EoA now — not needed until EoA capture. Overhead stays on for warmup.
    await eoa_cam.stop_streaming()
    print("[startup] Connecting lighting...")
    await lighting.connect()
    print("[startup] Connecting robot...")
    await robot.connect()
    await robot.move_to_safe_park()    # sensor warms during HOME (~14 s travel)
    await lighting.set_backlight(True)
    print("[startup] Ready — sensor warm, backlight on\n")
    print("  Dish robot XY range: X 10-110 mm, Y 180-270 mm")
    print("  Click inside the dish only.\n")

    calib = load_calibration(r"camera_calibration\output\calibration_overhead.npy")

    # ── Take first overhead snapshot ──────────────────────────────────────────
    # stop_streaming first: flushes any frames buffered during connect().
    # Then start_streaming + drain 2 + keep 1 — exactly like _run_cycle().
    _status["text"]   = "Capturing overhead snapshot..."
    _status["colour"] = (0, 165, 255)
    await overhead.stop_streaming()
    frame = await _capture_overhead_snapshot(overhead)
    _put(_frame_q, _to_display(frame))
    _status["text"]   = "Click a bug to move robot there  (R=refresh, H=home, Q=quit)"
    _status["colour"] = (0, 255, 0)

    # ── Robot keepalive task ──────────────────────────────────────────────────
    # sort_server.prg drops the TCP connection after ~5 s idle.
    # Send STATUS every 2 s while the user is deciding where to click.
    _keepalive_active = True

    async def _keepalive_task() -> None:
        while _keepalive_active and not _done.is_set():
            await asyncio.sleep(2.0)
            try:
                await robot.keepalive()
            except Exception:
                pass

    keepalive_task = asyncio.create_task(_keepalive_task())

    # ── Command loop ──────────────────────────────────────────────────────────
    # Use get_nowait + asyncio.sleep so we yield to the event loop each iteration.
    # (blocking queue.get(timeout=...) would starve asyncio tasks like keepalive)
    try:
        while not _done.is_set():
            try:
                msg = _click_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)   # yield — lets keepalive run
                continue

            cmd = msg[0]

            if cmd == "quit":
                break

            elif cmd == "home":
                _status["text"]   = "Homing robot..."
                _status["colour"] = (0, 165, 255)
                await robot.move_to_safe_park()
                _status["text"]   = "Homed — click a bug"
                _status["colour"] = (0, 255, 0)

            elif cmd == "refresh":
                _status["text"]   = "Refreshing overhead..."
                _status["colour"] = (0, 165, 255)
                await lighting.set_backlight(True)
                frame = await _capture_overhead_snapshot(overhead)
                _put(_frame_q, _to_display(frame))
                _status["text"]   = "Click a bug to move robot there"
                _status["colour"] = (0, 255, 0)

            elif cmd == "click":
                _, px, py = msg
                rx, ry = calib.pixel_to_robot(px, py)
                print(f"[click]  pixel ({px:.0f}, {py:.0f})  ->  robot ({rx:.3f}, {ry:.3f}) mm")

                # Reject clicks outside the dish workspace
                if not (-10 <= rx <= 130 and 160 <= ry <= 290):
                    info = f"SKIPPED ({rx:.1f}, {ry:.1f}) mm — click inside the dish"
                    print(f"[bounds] {info}")
                    _status["text"]   = info
                    _status["colour"] = (0, 0, 255)
                    continue

                _status["text"]   = f"Moving to ({rx:.1f}, {ry:.1f}) mm..."
                _status["colour"] = (0, 165, 255)

                try:
                    # 1. Backlight OFF confirmed before robot starts moving.
                    #    Driver waits 0.1s after sending OFF so LEDs are dark.
                    await lighting.set_backlight(False)
                    print("[light]  backlight OFF")

                    # 2. Robot moves to EoA position (no cameras streaming).
                    await robot.move_to_image_site(rx, ry)
                    print("[robot]  at image position")

                    # 3. Ring light ON (robot DO 12) — backlight already off.
                    try:
                        await robot.ring_light_on()
                        print("[ring]   ON")
                    except Exception as exc:
                        print(f"[ring]   ring_light_on failed: {exc}")

                    # 4. EoA capture — backlight confirmed off, ring light on.
                    _status["text"] = "Capturing EoA..."
                    await eoa_cam.start_streaming()
                    await asyncio.sleep(1.0)
                    await eoa_cam.capture()   # drain stale 1
                    await eoa_cam.capture()   # drain stale 2
                    eoa_raw = await eoa_cam.capture()
                    await eoa_cam.stop_streaming()
                    print("[eoa]    captured — check EoA window")

                    # 5. Ring light OFF before robot moves.
                    try:
                        await robot.ring_light_off()
                        print("[ring]   OFF")
                    except Exception as exc:
                        print(f"[ring]   ring_light_off failed: {exc}")

                    eoa_disp = _to_display(eoa_raw)
                    label = (f"EoA at robot ({rx:.2f}, {ry:.2f}) mm"
                             f"  <-  pixel ({px:.0f}, {py:.0f})")
                    _put(_eoa_q, _annotate(eoa_disp, label, (0, 255, 255)))

                    # 6. Robot returns home.
                    await robot.move_to_safe_park()

                    # 7. Backlight ON confirmed before overhead snapshot.
                    #    Driver waits 1.0s after sending ON so LEDs are at full brightness.
                    await lighting.set_backlight(True)
                    print("[light]  backlight ON")

                    # 8. Refresh overhead snapshot — backlight confirmed stable.
                    _status["text"]   = "Refreshing overhead..."
                    _status["colour"] = (0, 165, 255)
                    frame = await _capture_overhead_snapshot(overhead)
                    _put(_frame_q, _to_display(frame))

                except Exception as exc:
                    print(f"[error]  {exc}")
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    try: await robot.ring_light_off()
                    except Exception: pass
                    try:
                        await asyncio.gather(
                            lighting.set_backlight(True),
                            robot.move_to_safe_park(),
                        )
                    except Exception: pass

                finally:
                    _status["text"]   = "Click next bug  (R=refresh, H=home, Q=quit)"
                    _status["colour"] = (0, 255, 0)

    finally:
        _keepalive_active = False
        keepalive_task.cancel()
        print("\n[shutdown] cleaning up...")
        _status["text"] = "Shutting down..."
        for coro in [
            robot.ring_light_off(),
            robot.move_to_safe_park(),
            robot.disconnect(),
            lighting.set_backlight(False),
            lighting.disconnect(),
            overhead.stop_streaming(),
            overhead.disconnect(),
            eoa_cam.stop_streaming(),
            eoa_cam.disconnect(),
        ]:
            try: await coro
            except Exception: pass
        print("[shutdown] done")
        _done.set()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_main())

    t = threading.Thread(target=_run, daemon=True, name="async")
    t.start()

    _display_main()

    t.join(timeout=8.0)


if __name__ == "__main__":
    main()
