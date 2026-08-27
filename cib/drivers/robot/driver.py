"""
Real Epson T3-B robot driver — TCP socket to sort_server.prg on robot controller.

Protocol (implemented by robot_programs/sort_server.prg running in RC+ 7.0):
    Python sends:  CMD\r\n
    Robot replies: response\r\n  (sent AFTER motion completes)

All sends block until the robot sends its response — so every await call in
this driver returns only when the robot has physically finished the commanded
motion. Never call two methods concurrently.

Commands used:
    STATUS              -> OK READY
    HOME                -> OK HOME         (lift Z, always ends Righty at right home)
    GO_HOME_LEFT         -> OK GO_HOME_LEFT (lift Z, always ends Lefty at left home)
    MOVE_IMAGE rx,ry    -> OK IMAGE_POSITION rx,ry   (XY + drop Z for EoA, holds current U/hand)
    MOVE_IMAGE_U rx,ry,u,hand -> OK IMAGE_POSITION rx,ry  (explicit U + hand — quadrant selection)
    PICK rx,ry          -> OK PICKED rx,ry           (pipette Tool 2, aspirate, holds current U/hand)
    PICK_U rx,ry,u,hand -> OK PICKED rx,ry           (explicit U + hand — quadrant selection)
    DROP rx,ry          -> OK DROPPED rx,ry          (pipette Tool 2, dispense, holds current U/hand)
    DROP_U rx,ry,u,hand -> OK DROPPED rx,ry          (explicit U + hand — always the current
                                                       side's HOME U, never a quadrant-pick U)
    BYE                 -> OK BYE

hand is "L" or "R" — only meaningful on the *_U commands (the non-U
variants hold whatever hand the arm is currently in, no hand field at
all). Defaults to "R" everywhere below for backward compatibility with
single-hand/Righty-only callers.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000

# Timeout for STATUS / BYE commands (short — no motion)
CONNECT_TIMEOUT_S = 5.0

# Timeout for motion commands — robot moves at 5% speed so long travel can
# take 60+ seconds. 120 s gives plenty of margin.
MOTION_TIMEOUT_S = 120.0


class EpsonT3BDriver:
    """
    Epson T3-B SCARA robot driver — TCP socket to sort_server.prg.

    sort_server.prg must be loaded and running in RC+ 7.0 on the robot
    controller before connect() is called.
    """

    def __init__(
        self,
        ip: str = ROBOT_IP,
        port: int = ROBOT_PORT,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        motion_timeout_s: float = MOTION_TIMEOUT_S,
    ) -> None:
        self._ip = ip
        self._port = port
        self._connect_timeout = connect_timeout_s
        self._motion_timeout = motion_timeout_s
        self._sock: socket.socket | None = None
        self._halted = False
        # Set the moment disconnect() is called. Once True, the reconnect-
        # on-drop logic in every _xxx_sync method (and the keepalive loop)
        # stops trying to reconnect+HOME — a broken connection during
        # shutdown should just fail cleanly, not open a fresh connection and
        # issue new motion commands while the system is tearing down.
        self._shutting_down = False
        # Serialises all send/recv exchanges so the keepalive loop cannot
        # interleave with a motion command running in a different thread.
        # RLock (re-entrant) because _reconnect_sync is called from within
        # _move_image_sync which already holds the lock.
        self._cmd_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        log.info("robot.connecting", ip=self._ip, port=self._port)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self._connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # TCP keepalive — OS sends ACK probes after idle, every interval below.
        # Prevents the Epson controller's TCP stack from timing out the
        # connection during long overhead-capture + vision processing gaps.
        # Idle threshold raised 5s -> 60s (2026-08-27 real-hardware finding):
        # a slow (Speed 5/Accel 5,5, per operator request) HOME/GO_HOME_LEFT
        # right_home<->left_home crossing can genuinely take longer than the
        # old 5s-idle-then-probe / ~15-20s-total-before-giving-up window --
        # Python's blocking _send() sits waiting the whole time with zero
        # bytes flowing, so Windows' own OS-level keepalive was declaring the
        # (perfectly healthy, just busy) connection dead and killing the
        # socket underneath it -- no SPEL+ error ever fires, since nothing
        # actually failed on the robot side. 60s comfortably covers any
        # single motion command (matches MOTION_TIMEOUT_S's own 120s
        # philosophy) while still catching a truly dead peer well within
        # that -- the STATUS-based application-level keepalive loop already
        # handles the "genuinely idle between commands" case in a few
        # seconds regardless, this OS-level one is just a backstop.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            # Windows: (enable, idle_ms before first probe, interval_ms)
            s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 5_000))
        except (AttributeError, OSError):
            pass  # non-Windows or older Python — SO_KEEPALIVE above still applies

        try:
            s.connect((self._ip, self._port))
        except OSError as exc:
            raise RuntimeError(
                f"Cannot connect to robot at {self._ip}:{self._port} — {exc}\n"
                "Check: robot powered on, sort_server.prg running in RC+ (F5)."
            ) from exc
        self._sock = s

        # Verify robot is ready
        with self._cmd_lock:
            resp = self._send("STATUS", timeout=self._connect_timeout)
        if "OK READY" not in resp:
            raise RuntimeError(
                f"Robot STATUS returned unexpected response: {resp!r}\n"
                "sort_server.prg must be running in RC+ (F5)."
            )
        # Surfaces sort_server.prg's version tag in the log automatically on
        # every connect, so a stale download-over-a-running-task shows up
        # immediately instead of needing a manual STATUS query to catch.
        log.info("robot.connected", ip=self._ip, port=self._port, controller_response=resp)

    async def disconnect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        self._shutting_down = True
        if self._sock is None:
            return
        with self._cmd_lock:
            try:
                self._send("BYE", timeout=self._connect_timeout)
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        log.info("robot.disconnected")

    async def is_ready(self) -> bool:
        return self._sock is not None and not self._halted

    # ------------------------------------------------------------------
    # Motion commands — called by orchestrator
    # ------------------------------------------------------------------

    async def move_to_image_site(
        self, x: float, y: float, u: float | None = None, hand: str = "R",
    ) -> None:
        """
        Move EoA camera above the specimen at (x, y) for imaging.

        Motion sequence (handled by sort_server.prg):
          1. Move linearly to (x, y, Z_SAFE=-47.700)   — safe travel height
          2. Move linearly down to (x, y, Z_IMAGING=-58.660) — EoA imaging height

        u: optional wrist angle (deg) for quadrant-based U selection. If given,
        sends MOVE_IMAGE_U (explicit U + hand). If None, sends MOVE_IMAGE (holds
        whatever U/hand the robot is currently at) — hand is ignored in that case.

        hand: "L" or "R" — which side of the dish this quadrant U belongs to.
        Only meaningful when u is given (the non-U command has no hand field).

        Blocks until robot is stationary at imaging height (robot sends response
        only after both moves complete). Then Python triggers camera capture.
        """
        log.info("robot.move_to_image_site", x=x, y=y, u=u, hand=hand)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._move_image_sync,
            x, y, u, hand,
        )

    def _move_image_sync(
        self, x: float, y: float, u: float | None = None, hand: str = "R",
    ) -> None:
        cmd = (
            f"MOVE_IMAGE_U {x:.3f},{y:.3f},{u:.3f},{hand}"
            if u is not None
            else f"MOVE_IMAGE {x:.3f},{y:.3f}"
        )
        with self._cmd_lock:
            try:
                resp = self._send(cmd, timeout=self._motion_timeout)
            except (ConnectionError, OSError) as exc:
                # TCP connection was dropped by the Epson controller (idle timeout).
                # sort_server.prg now restarts its TCP listener on disconnect, so
                # reconnect and retry the command once.
                log.warning("robot.reconnecting_after_drop", reason=str(exc))
                self._reconnect_sync()
                resp = self._send(cmd, timeout=self._motion_timeout)
        if resp.startswith("ERR MOTION_FAILED"):
            raise RuntimeError(
                "Robot Joint 2 limit — arm configuration wrong for this position. "
                "In sort_server.prg change 'Arm Righty' to 'Arm Lefty' and reload."
            )
        if not resp.startswith("OK IMAGE_POSITION"):
            raise RuntimeError(f"MOVE_IMAGE failed: {resp!r}")
        log.info("robot.at_image_position", response=resp)

    async def move_to_safe_park(self) -> None:
        """
        Return robot to home position.
        sort_server.prg lifts Z to safe height before going home.
        """
        log.info("robot.moving_to_home")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._home_sync)

    def _home_sync(self) -> None:
        with self._cmd_lock:
            try:
                resp = self._send("HOME", timeout=self._motion_timeout)
            except (ConnectionError, OSError) as exc:
                log.warning("robot.reconnecting_after_drop", reason=str(exc))
                self._reconnect_sync()
                resp = self._send("HOME", timeout=self._motion_timeout)
        if "OK HOME" not in resp:
            raise RuntimeError(f"HOME failed: {resp!r}")
        log.info("robot.at_home")

    async def go_home_left(self) -> None:
        """
        Return robot to LEFT home position (Lefty hand) — mirrors
        move_to_safe_park()/HOME above but for the left-side stations.
        sort_server.prg's GO_HOME_LEFT lifts Z if needed and always ends
        Lefty at left_home, using the same validated hand-crossing pattern
        as HOME (settle at whichever home matches the current hand first,
        then one continuous Go to cross).
        """
        log.info("robot.moving_to_home_left")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._home_left_sync)

    def _home_left_sync(self) -> None:
        with self._cmd_lock:
            try:
                resp = self._send("GO_HOME_LEFT", timeout=self._motion_timeout)
            except (ConnectionError, OSError) as exc:
                log.warning("robot.reconnecting_after_drop", reason=str(exc))
                self._reconnect_sync()
                resp = self._send("GO_HOME_LEFT", timeout=self._motion_timeout)
        if "OK GO_HOME_LEFT" not in resp:
            raise RuntimeError(f"GO_HOME_LEFT failed: {resp!r}")
        log.info("robot.at_home_left")

    async def execute_pick(
        self, pick_x: float, pick_y: float, u: float | None = None, hand: str = "R",
    ) -> None:
        """
        Aspirate the specimen at (pick_x, pick_y) using the pipette (Tool 2).
        Returns once the specimen is confirmed in the tip.

        u: wrist angle for the PICK move (dish quadrant U — see
           cib/core/orchestrator.py _select_quadrant_u()).
        hand: "L" or "R" — which hand this quadrant U belongs to. Only
              meaningful when u is given.

        Split from execute_place() so the orchestrator can durably record
        "specimen is now in the pipette" the instant this returns — needed
        so an e-stop/light-curtain fault after this point knows to deliver
        the specimen on recovery instead of losing it or attempting a
        second pick on top of it.
        """
        log.info("robot.execute_pick", pick_x=pick_x, pick_y=pick_y, u=u, hand=hand)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._pick_sync, pick_x, pick_y, u, hand)

    async def execute_place(
        self, well_x: float, well_y: float, well_u: float | None = None, hand: str = "R",
    ) -> None:
        """
        Travel to (well_x, well_y) and dispense the specimen currently held
        in the pipette (Tool 2).

        well_u: wrist angle for the DROP move. The plate station is a
                physically different location from the dish, almost
                certainly needing a different U — do NOT reuse the pick's u
                here. None falls back to holding whatever U the arm is
                already at (rarely correct once a real plate config is wired in).
                Caller must always pass the current side's HOME U here (never
                a quadrant-pick U) — hard safety rule, see
                cib/core/orchestrator.py's drop-U handling.
        hand: "L" or "R" — which side's plate station this is. Only
              meaningful when well_u is given.
        """
        log.info("robot.execute_place", well_x=well_x, well_y=well_y, well_u=well_u, hand=hand)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._drop_sync, well_x, well_y, well_u, hand)

    def _pick_sync(self, x: float, y: float, u: float | None = None, hand: str = "R") -> None:
        cmd = (
            f"PICK_U {x:.3f},{y:.3f},{u:.3f},{hand}"
            if u is not None
            else f"PICK {x:.3f},{y:.3f}"
        )
        with self._cmd_lock:
            try:
                resp = self._send(cmd, timeout=self._motion_timeout)
            except (ConnectionError, OSError) as exc:
                log.warning("robot.reconnecting_after_drop", reason=str(exc))
                self._reconnect_sync()
                resp = self._send(cmd, timeout=self._motion_timeout)
        if not resp.startswith("OK PICKED"):
            raise RuntimeError(f"PICK failed: {resp!r}")
        log.info("robot.pick_complete", response=resp)

    def _drop_sync(self, x: float, y: float, u: float | None = None, hand: str = "R") -> None:
        cmd = (
            f"DROP_U {x:.3f},{y:.3f},{u:.3f},{hand}"
            if u is not None
            else f"DROP {x:.3f},{y:.3f}"
        )
        with self._cmd_lock:
            try:
                resp = self._send(cmd, timeout=self._motion_timeout)
            except (ConnectionError, OSError) as exc:
                log.warning("robot.reconnecting_after_drop", reason=str(exc))
                self._reconnect_sync()
                resp = self._send(cmd, timeout=self._motion_timeout)
        if not resp.startswith("OK DROPPED"):
            raise RuntimeError(f"DROP failed: {resp!r}")
        log.info("robot.drop_complete", response=resp)

    async def halt(self) -> None:
        """
        Immediate software halt — called on safety fault.
        Closes TCP connection so sort_server.prg gets a Line Input error and exits.
        The hardware safety circuit (Flexi e-stop) has already stopped the robot.
        """
        self._halted = True
        log.warning("robot.halt_called")
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    async def ring_light_on(self) -> None:
        """Turn ring light ON via robot digital output 12."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ring_on_sync)

    def _ring_on_sync(self) -> None:
        # Use connect_timeout (5 s) not motion_timeout — this is a digital IO
        # toggle, not a motion command. sort_server.prg must handle "RING_ON".
        # Deliberately NO reconnect-on-drop here (unlike motion commands) —
        # a light toggle isn't worth reopening a connection and potentially
        # issuing new commands over. Caller already treats this as best-effort.
        with self._cmd_lock:
            resp = self._send("RING_ON", timeout=self._connect_timeout)
        if "OK RING_ON" not in resp:
            raise RuntimeError(f"RING_ON failed: {resp!r}")
        log.info("robot.ring_light_on")

    async def ring_light_off(self) -> None:
        """Turn ring light OFF via robot digital output 12."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ring_off_sync)

    def _ring_off_sync(self) -> None:
        # Deliberately NO reconnect-on-drop here — see _ring_on_sync. This
        # matters most during shutdown: ring_light_off() is the first thing
        # the orchestrator calls when tearing down, and reconnecting here
        # would open a fresh connection (and, via _reconnect_sync's own
        # re-home step) issue a new motion command in the middle of what's
        # supposed to be a clean shutdown.
        with self._cmd_lock:
            resp = self._send("RING_OFF", timeout=self._connect_timeout)
        if "OK RING_OFF" not in resp:
            raise RuntimeError(f"RING_OFF failed: {resp!r}")
        log.info("robot.ring_light_off")

    def resume(self) -> None:
        """Clear halted flag after safety reset. Call connect() separately to reconnect."""
        self._halted = False

    async def keepalive(self) -> None:
        """
        Send STATUS to prevent sort_server.prg from closing an idle TCP connection.

        sort_server.prg drops the connection if no command arrives within ~5 s of
        the previous exchange. Call this every 2–3 s while the robot is idle.
        Uses a non-blocking lock attempt — skips silently if a motion command is
        already in progress so the two never interleave on the TCP stream.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._keepalive_sync)

    def _keepalive_sync(self) -> None:
        """
        Send STATUS to keep sort_server.prg from closing an idle connection,
        and reconnect automatically if the Epson controller has already sent RST.

        The Epson controller's built-in TCP idle timer (~2.5 s) can fire when
        LWF delays ACKs/packets from Python. When that happens sort_server.prg
        jumps to client_gone: → CloseNet → restarts its listener. This method
        detects the closed socket and reconnects so the system self-heals without
        operator intervention.

        Uses non-blocking lock: skips silently if a motion command is running.
        """
        acquired = self._cmd_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            if self._halted or self._shutting_down:
                return

            # ── Reconnect path ───────────────────────────────────────────────
            if self._sock is None:
                # Connection was previously dropped (WinError 10054 from Epson
                # RST, or explicit close). sort_server.prg has already restarted
                # its TCP listener (GoTo serverloop → OpenNet → WaitNet).
                # Reconnect now so the system is ready before the next command.
                try:
                    self._connect_sync()
                    log.info("robot.keepalive_reconnected")
                except Exception as exc:
                    log.debug("robot.keepalive_reconnect_failed", error=str(exc))
                return  # don't send STATUS on the same tick as reconnect

            # ── Keepalive path ───────────────────────────────────────────────
            self._send("STATUS", timeout=self._connect_timeout)
            log.debug("robot.keepalive_ok")

        except Exception as exc:
            log.debug("robot.keepalive_error", error=str(exc))
            # Close so the TCP buffer is discarded and the next tick takes the
            # reconnect path above instead of sending into a dead socket.
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        finally:
            self._cmd_lock.release()

    def _reconnect_sync(self) -> None:
        """
        Close the dead socket and reconnect to sort_server.prg.

        sort_server.prg (updated) automatically restarts its TCP listener after
        a connection drop, so this will succeed without user intervention in RC+.
        After reconnect, also sends HOME to lift Z to safe position — the robot
        may be anywhere if the previous move was interrupted mid-motion.

        Raises ConnectionError immediately if shutdown has started, or if
        halt() has been called — never reconnect (and never issue a fresh
        HOME motion command) while the system is tearing down OR while a
        safety fault has the robot halted. halt() closes the socket, which
        makes any command already in flight land here; without this guard,
        that would silently reopen the connection and re-home the robot
        mid-e-stop, before the safety system has actually cleared.
        """
        if self._shutting_down:
            raise ConnectionError("Robot disconnected — shutdown in progress, not reconnecting")
        if self._halted:
            raise ConnectionError("Robot disconnected — halted by safety fault, not reconnecting")
        log.warning("robot.reconnect_start")
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        self._connect_sync()  # re-open socket + verify STATUS
        # Re-home: lifts Z to safe height so the interrupted MOVE_IMAGE
        # doesn't leave the arm at imaging height before the next Go XY.
        with self._cmd_lock:
            resp = self._send("HOME", timeout=self._motion_timeout)
        if "OK HOME" not in resp:
            log.warning("robot.reconnect_home_failed", resp=resp)
        log.warning("robot.reconnect_complete")

    # ------------------------------------------------------------------
    # TCP helpers
    # ------------------------------------------------------------------

    def _send(self, cmd: str, timeout: float) -> str:
        """
        Send command string, block until one response line is received.
        All communication is CR+LF terminated per SPEL+ Print #201 format.
        """
        if self._sock is None:
            raise ConnectionError("Robot not connected — socket closed")

        self._sock.settimeout(timeout)
        try:
            self._sock.sendall((cmd + "\r\n").encode("ascii"))
            return self._recv_line()
        except socket.timeout as exc:
            raise RuntimeError(
                f"Robot timed out after {timeout}s waiting for response to: {cmd!r}"
            ) from exc

    def _recv_line(self) -> str:
        """Read exactly one CRLF-terminated line from the robot socket."""
        buf = b""
        while True:
            chunk = self._sock.recv(1)
            if not chunk:
                raise ConnectionError("Robot closed the connection unexpectedly")
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return buf.decode("ascii", errors="replace").strip()
