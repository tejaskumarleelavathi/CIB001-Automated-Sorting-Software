#!/usr/bin/env python3
"""
hardware_tests/test_pipette.py  —  Interactive pipette pick / drop tester
=========================================================================

Pre-requisites:
  1. Load pipette_test.prg in RC+ (NOT sort_server.prg) and run it (F5).
  2. Run this script from the repo root:
         python hardware_tests/test_pipette.py

Flow:
  1. Overhead camera captures a snapshot of the dish.
  2. Left-click any bug -> robot moves there (MOVE_TO), pipette stays UP.
  3. Press A -> robot commands ASPIRE  (pipette DOWN, motor draws back, pipette UP).
  4. Press D -> robot commands DROP    (pipette DOWN, motor pushes forward, pipette UP).
  5. Press H -> robot goes HOME.
  6. Press R -> refresh overhead snapshot.
  7. Press Q or ESC -> home robot and quit.

Status banner is printed in the window title bar and as an overlay on the image.

IMPORTANT:
  - pipette_test.prg uses port 2000 — the same port as sort_server.prg.
    Only ONE program can be running at a time.
  - The robot does NOT move until pipette is confirmed UP (Input 20 = ON).
  - ASPIRE / DROP do NOT start until pipette is confirmed DOWN (Input 22 = ON).
  - On Python disconnect the .prg forces Output 4 OFF (pipette retracted).
"""
from __future__ import annotations

import asyncio
import queue
import socket
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

DISPLAY_SCALE = 0.24          # 5320×4600 -> ~1277×1104 display
ROBOT_IP      = "172.16.9.15"
ROBOT_PORT    = 2000
CMD_TIMEOUT_S = 120.0         # long timeout for motion + motor commands

# ── Inter-thread state ────────────────────────────────────────────────────────
_frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
_cmd_q:   "queue.Queue"             = queue.Queue()
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


# ── Raw socket to pipette_test.prg ───────────────────────────────────────────

class PipetteRobot:
    """
    Thin TCP client for pipette_test.prg.

    pipette_test.prg is a TCP server on port 2000 that accepts:
        STATUS          -> OK READY
        HOME            -> OK HOME
        MOVE_TO x,y     -> OK AT_POSITION x,y
        ASPIRE          -> OK ASPIRATED
        DROP            -> OK DROPPED
        BYE             -> OK BYE
    """

    def __init__(self, ip: str = ROBOT_IP, port: int = ROBOT_PORT) -> None:
        self._ip   = ip
        self._port = port
        self._sock: socket.socket | None = None
        self._buf  = b""
        self._lock = threading.Lock()

    def connect(self) -> None:
        print(f"[robot] Connecting to {self._ip}:{self._port} (pipette_test.prg)...")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET,  socket.SO_KEEPALIVE, 1)
        try:
            s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5_000, 2_000))
        except (AttributeError, OSError):
            pass
        try:
            s.connect((self._ip, self._port))
        except OSError as exc:
            raise RuntimeError(
                f"Cannot connect to {self._ip}:{self._port} — {exc}\n"
                "Check: pipette_test.prg is loaded and running in RC+ (F5).\n"
                "       (NOT sort_server.prg — they share the same port 2000)"
            ) from exc
        self._sock = s
        resp = self._send("STATUS", timeout=10.0)
        if "OK READY" not in resp:
            raise RuntimeError(
                f"STATUS returned unexpected: {resp!r}\n"
                "Make sure pipette_test.prg is running, not sort_server.prg."
            )
        print("[robot] Connected — OK READY")

    def disconnect(self) -> None:
        if self._sock is None:
            return
        try:
            self._send("BYE", timeout=5.0)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        print("[robot] Disconnected")

    def _recv_line(self, timeout: float) -> str:
        """Read one CR/LF-terminated line from socket."""
        assert self._sock is not None
        self._sock.settimeout(timeout)
        while b"\n" not in self._buf and b"\r" not in self._buf:
            chunk = self._sock.recv(256)
            if not chunk:
                raise ConnectionError("Robot closed the connection")
            self._buf += chunk
        # Split on CR or LF (SPEL+ sends \r\n)
        for sep in (b"\r\n", b"\n", b"\r"):
            if sep in self._buf:
                line, self._buf = self._buf.split(sep, 1)
                return line.decode(errors="replace").strip()
        raise RuntimeError("Should not reach here")

    def _send(self, cmd: str, timeout: float = CMD_TIMEOUT_S) -> str:
        assert self._sock is not None
        self._sock.sendall((cmd + "\r\n").encode())
        return self._recv_line(timeout)

    def send_status(self) -> str:
        with self._lock:
            return self._send("STATUS", timeout=5.0)

    def send_home(self) -> str:
        with self._lock:
            print("[robot] HOME")
            resp = self._send("HOME", timeout=CMD_TIMEOUT_S)
            print(f"[robot] <- {resp}")
            if "OK HOME" not in resp:
                raise RuntimeError(f"HOME failed: {resp!r}")
            return resp

    def send_move_to(self, rx: float, ry: float) -> str:
        with self._lock:
            cmd = f"MOVE_TO {rx:.3f},{ry:.3f}"
            print(f"[robot] {cmd}")
            resp = self._send(cmd, timeout=CMD_TIMEOUT_S)
            print(f"[robot] <- {resp}")
            if not resp.startswith("OK AT_POSITION"):
                raise RuntimeError(f"MOVE_TO failed: {resp!r}")
            return resp

    def send_aspire(self) -> str:
        with self._lock:
            print("[robot] ASPIRE")
            resp = self._send("ASPIRE", timeout=CMD_TIMEOUT_S)
            print(f"[robot] <- {resp}")
            if "OK ASPIRATED" not in resp:
                raise RuntimeError(f"ASPIRE failed: {resp!r}")
            return resp

    def send_drop(self) -> str:
        with self._lock:
            print("[robot] DROP")
            resp = self._send("DROP", timeout=CMD_TIMEOUT_S)
            print(f"[robot] <- {resp}")
            if "OK DROPPED" not in resp:
                raise RuntimeError(f"DROP failed: {resp!r}")
            return resp


# ── Display — main thread ─────────────────────────────────────────────────────

def _display_main() -> None:
    WIN = "Pipette Test  (click bug to move robot)"
    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)

    dh, dw = int(4600 * DISPLAY_SCALE), int(5320 * DISPLAY_SCALE)
    frame = np.zeros((dh, dw, 3), dtype=np.uint8)

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            _cmd_q.put(("move", x / DISPLAY_SCALE, y / DISPLAY_SCALE))

    cv2.setMouseCallback(WIN, on_mouse)
    hint = "L-click: move robot  |  A: aspire  |  D: drop  |  H: home  |  R: refresh  |  Q: quit"

    while not _done.is_set():
        try: frame = _frame_q.get_nowait()
        except queue.Empty: pass

        disp = _annotate(frame, _status["text"], _status["colour"])
        cv2.putText(disp, hint, (10, disp.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 160, 160), 1)
        cv2.imshow(WIN, disp)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), 27):         # Q or ESC
            _cmd_q.put(("quit",))
            _done.set()
        elif key in (ord("a"), ord("A")):
            _cmd_q.put(("aspire",))
        elif key in (ord("d"), ord("D")):
            _cmd_q.put(("drop",))
        elif key in (ord("h"), ord("H")):
            _cmd_q.put(("home",))
        elif key in (ord("r"), ord("R")):
            _cmd_q.put(("refresh",))

    cv2.destroyAllWindows()


# ── Async — background thread ─────────────────────────────────────────────────

async def _capture_overhead(overhead) -> np.ndarray:
    """Drain 2 stale frames, keep 1 — identical to main pipeline."""
    await overhead.start_streaming()
    await overhead.capture()   # stale 1
    await overhead.capture()   # stale 2
    frame = await overhead.capture()
    await overhead.stop_streaming()
    return frame


async def _async_main() -> None:
    from cib.logging_setup import configure as configure_logging
    from cib.drivers.camera.driver import GigECameraDriver
    from cib.vision.calibration    import load_calibration
    from cib.config.loader         import load_machine_config

    configure_logging()

    _prod = Path(r"C:\LabAutomation\config\machine_config.yaml")
    _dev  = Path(__file__).parent.parent / "config" / "machine_config.yaml"
    cfg   = load_machine_config(_prod if _prod.exists() else _dev)

    oh_cfg = cfg.overhead_camera
    overhead = GigECameraDriver(
        serial=oh_cfg.serial, target_ip=oh_cfg.ip,
        exposure_us=oh_cfg.exposure_us, gain_db=oh_cfg.gain_db,
        pixel_format=oh_cfg.pixel_format,
        max_fps=getattr(oh_cfg, "max_fps", None), name="overhead",
    )

    loop = asyncio.get_running_loop()

    # ── Connect overhead camera ───────────────────────────────────────────────
    print("[startup] Connecting overhead camera...")
    await overhead.connect()

    # ── Connect robot (raw socket — pipette_test.prg) ────────────────────────
    robot = PipetteRobot(ip=cfg.robot.ip, port=cfg.robot.port)
    await loop.run_in_executor(None, robot.connect)

    # ── Load overhead calibration ─────────────────────────────────────────────
    calib = load_calibration(r"camera_calibration\output\calibration_overhead.npy")

    # ── First snapshot ────────────────────────────────────────────────────────
    _status["text"]   = "Capturing overhead snapshot..."
    _status["colour"] = (0, 165, 255)
    await overhead.stop_streaming()
    frame = await _capture_overhead(overhead)
    _put(_frame_q, _to_display(frame))
    _status["text"]   = "Click a bug to move robot there"
    _status["colour"] = (0, 255, 0)
    print("[startup] Ready — overhead image captured\n")
    print("  Controls: left-click = MOVE_TO | A = ASPIRE | D = DROP | H = HOME | R = refresh | Q = quit\n")

    # ── Keepalive ─────────────────────────────────────────────────────────────
    _keepalive_active = True

    async def _keepalive() -> None:
        while _keepalive_active and not _done.is_set():
            await asyncio.sleep(2.0)
            try:
                await loop.run_in_executor(None, robot.send_status)
            except Exception:
                pass

    keepalive = asyncio.create_task(_keepalive())

    # Track where robot currently is so ASPIRE/DROP are only valid after MOVE_TO
    last_position: tuple[float, float] | None = None

    # ── Command loop ──────────────────────────────────────────────────────────
    try:
        while not _done.is_set():
            try:
                msg = _cmd_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue

            cmd = msg[0]

            if cmd == "quit":
                break

            elif cmd == "home":
                _status["text"]   = "Homing robot..."
                _status["colour"] = (0, 165, 255)
                try:
                    await loop.run_in_executor(None, robot.send_home)
                    last_position = None
                    _status["text"]   = "Homed — click a bug"
                    _status["colour"] = (0, 255, 0)
                except Exception as exc:
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    print(f"[error] {exc}")

            elif cmd == "refresh":
                _status["text"]   = "Refreshing overhead..."
                _status["colour"] = (0, 165, 255)
                try:
                    frame = await _capture_overhead(overhead)
                    _put(_frame_q, _to_display(frame))
                    _status["text"]   = "Click a bug to move robot there"
                    _status["colour"] = (0, 255, 0)
                except Exception as exc:
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    print(f"[error] {exc}")

            elif cmd == "move":
                _, px, py = msg
                rx, ry = calib.pixel_to_robot(px, py)
                print(f"[click]  pixel ({px:.0f}, {py:.0f})  ->  robot ({rx:.3f}, {ry:.3f}) mm")

                # Bounds check — reject clicks outside dish workspace
                if not (-10 <= rx <= 130 and 160 <= ry <= 290):
                    info = f"SKIPPED ({rx:.1f}, {ry:.1f}) mm — click inside dish"
                    print(f"[bounds] {info}")
                    _status["text"]   = info
                    _status["colour"] = (0, 0, 255)
                    continue

                _status["text"]   = f"Moving to ({rx:.1f}, {ry:.1f}) mm..."
                _status["colour"] = (0, 165, 255)
                try:
                    await loop.run_in_executor(None, robot.send_move_to, rx, ry)
                    last_position = (rx, ry)
                    _status["text"]   = (
                        f"At ({rx:.1f}, {ry:.1f}) mm  |  A=aspire  D=drop  H=home"
                    )
                    _status["colour"] = (0, 255, 0)
                except Exception as exc:
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    print(f"[error] {exc}")
                    last_position = None

            elif cmd == "aspire":
                if last_position is None:
                    info = "Click a bug first — robot must be at a position before ASPIRE"
                    print(f"[aspire] {info}")
                    _status["text"]   = info
                    _status["colour"] = (0, 0, 255)
                    continue
                _status["text"]   = "Aspirating..."
                _status["colour"] = (0, 165, 255)
                try:
                    await loop.run_in_executor(None, robot.send_aspire)
                    rx, ry = last_position
                    _status["text"]   = f"Aspirated at ({rx:.1f}, {ry:.1f}) mm  |  D=drop  H=home"
                    _status["colour"] = (0, 255, 0)
                except Exception as exc:
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    print(f"[error] {exc}")

            elif cmd == "drop":
                if last_position is None:
                    info = "Click a bug first — robot must be at a position before DROP"
                    print(f"[drop]   {info}")
                    _status["text"]   = info
                    _status["colour"] = (0, 0, 255)
                    continue
                _status["text"]   = "Dropping..."
                _status["colour"] = (0, 165, 255)
                try:
                    await loop.run_in_executor(None, robot.send_drop)
                    rx, ry = last_position
                    _status["text"]   = f"Dropped at ({rx:.1f}, {ry:.1f}) mm  |  H=home or click next bug"
                    _status["colour"] = (0, 255, 0)
                except Exception as exc:
                    _status["text"]   = f"ERROR: {str(exc)[:80]}"
                    _status["colour"] = (0, 0, 255)
                    print(f"[error] {exc}")

    finally:
        _keepalive_active = False
        keepalive.cancel()
        print("\n[shutdown] cleaning up...")
        _status["text"]   = "Shutting down..."
        _status["colour"] = (0, 165, 255)
        try:
            await loop.run_in_executor(None, robot.send_home)
        except Exception:
            pass
        try:
            await loop.run_in_executor(None, robot.disconnect)
        except Exception:
            pass
        try:
            await overhead.stop_streaming()
        except Exception:
            pass
        try:
            await overhead.disconnect()
        except Exception:
            pass
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

    t.join(timeout=10.0)


if __name__ == "__main__":
    main()
