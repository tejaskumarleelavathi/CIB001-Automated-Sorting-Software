"""
Real lighting controller driver — Advanced Illumination DCS-100E.

Protocol confirmed from hardware_tests/test_lighting.py:
    Commands are semicolon-terminated ASCII, multiple commands batched in one send.
    Three channels (1, 2, 3) — all turned on/off together for the backlight.

    SET:MODE:CHANNELx,1      → turn channel x ON
    SET:MODE:CHANNELx,0      → turn channel x OFF
    SET:LEVEL:CHANNELx,{mA} → set channel x current in milliamps
    DISCONNECT               → clean close

Device:  172.16.9.20, port 777
Current: 250 mA per channel
"""

from __future__ import annotations

import asyncio
import socket
import time
import structlog

log = structlog.get_logger(__name__)

CHANNELS       = [1, 2, 3]    # DCS-100E has 3 channels — all used for backlight
RECV_TIMEOUT_S = 0.01         # non-blocking drain — SET commands need no ack
RECV_BUF       = 4096


class LightingControllerDriver:

    def __init__(
        self,
        ip: str = "172.16.9.20",
        port: int = 777,
        current_ma: int = 250,
        connect_timeout_s: float = 2.0,
    ) -> None:
        self._ip = ip
        self._port = port
        self._current_ma = current_ma
        self._connect_timeout = connect_timeout_s
        self._sock: socket.socket | None = None
        self._backlight_on = False

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        # The DCS-100E may reset the connection (WinError 10054 / ConnectionResetError)
        # immediately after a previous session was killed without sending DISCONNECT.
        # The reset can happen during connect() OR during the initial _send_batch()
        # commands — so the retry wraps the ENTIRE connect + init sequence.
        # DCS-100E supports only ONE TCP connection at a time.
        # If the previous session ended without DISCONNECT (process crash/kill), the
        # device's application layer still thinks the old session is active and sends RST
        # on new connections. It clears this automatically after its internal timeout
        # (~15s based on observed behaviour) — no power-cycle required.
        #
        # We retry patiently until the device is ready. Total budget = 30s, which is
        # well beyond the observed recovery time.
        #
        # In normal operation (SHUTDOWN button used) this path is never hit because
        # DISCONNECT is sent cleanly before exit.
        MAX_CONNECT_ATTEMPTS = 10
        RETRY_WAIT_S         = 3.0   # total budget = 27s between attempts

        last_err: Exception | None = None
        for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self._connect_timeout)
            reset_stage = "connect"
            try:
                sock.connect((self._ip, self._port))
                # TCP handshake succeeded — RST from here on is during data exchange
                reset_stage = "init_commands"
                self._sock = sock
                # Set current and turn OFF each channel individually,
                # draining each response so the buffer stays clean.
                for ch in CHANNELS:
                    self._send_command(f"SET:LEVEL:CHANNEL{ch},{self._current_ma}")
                    self._send_command(f"SET:MODE:CHANNEL{ch},0")
                self._backlight_on = False
                break   # success
            except ConnectionResetError as exc:
                try:
                    sock.close()
                except Exception:
                    pass
                self._sock = None
                last_err = exc
                elapsed = (attempt - 1) * RETRY_WAIT_S
                log.warning(
                    "lighting.connect_reset",
                    attempt=attempt,
                    of=MAX_CONNECT_ATTEMPTS,
                    elapsed_s=elapsed,
                    stage=reset_stage,   # "connect" = RST before handshake complete
                                         # "init_commands" = RST after handshake
                    note="device clearing previous session — will retry automatically",
                )
                if attempt < MAX_CONNECT_ATTEMPTS:
                    time.sleep(RETRY_WAIT_S)
        else:
            raise RuntimeError(
                f"Lighting controller at {self._ip}:{self._port} is not accepting "
                f"connections after {MAX_CONNECT_ATTEMPTS} attempts "
                f"({(MAX_CONNECT_ATTEMPTS - 1) * RETRY_WAIT_S:.0f}s). "
                f"Check that the DCS-100E is powered and reachable at that IP. "
                f"Last error: {last_err}"
            )

        log.info("lighting.connected", ip=self._ip, port=self._port,
                 current_ma=self._current_ma)

    async def disconnect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        if self._sock:
            try:
                # Turn all channels off individually, then disconnect cleanly
                for ch in CHANNELS:
                    self._send_command(f"SET:MODE:CHANNEL{ch},0")
                self._send_command("DISCONNECT")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._backlight_on = False
        log.info("lighting.disconnected")

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    async def set_backlight(self, on: bool) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_backlight_sync, on)

    def _set_backlight_sync(self, on: bool) -> None:
        if on == self._backlight_on:
            return  # already in the right state — no send, no wait

        mode = 1 if on else 0
        # Send all 3 channel commands in one TCP write — one packet, one drain.
        # Previously sent 3 separate _send_command() calls, each with its own
        # 10ms drain timeout = 3× round trips and variable 0–30ms total latency.
        # Batching eliminates that variability: all channels switch simultaneously,
        # one drain handles any response the device sends.
        self._send_batch([f"SET:MODE:CHANNEL{ch},{mode}" for ch in CHANNELS])

        self._backlight_on = on
        log.debug("lighting.backlight", on=on)

    async def set_ring_light(self, on: bool) -> None:
        """Ring light is robot digital output 12 — not controlled by DCS-100E."""
        log.debug("lighting.ring_light_noop", on=on,
                  note="ring light via robot digital output 12")

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _send_command(self, command: str) -> None:
        """Send a single command and drain its response (fire-and-forget)."""
        self._send_batch([command])

    def _send_batch(self, commands: list[str]) -> None:
        """
        Send multiple commands in one TCP write, each semicolon-terminated.
        Drain any response with a short timeout — response is not required.
        """
        if self._sock is None:
            raise RuntimeError("Lighting controller not connected")

        payload = "".join(
            cmd if cmd.endswith(";") else cmd + ";"
            for cmd in commands
        )
        self._sock.sendall(payload.encode("ascii"))

        # Drain loop — read until no new data arrives within RECV_TIMEOUT_S.
        # A single recv() only reads ONE TCP segment. The DCS-100E sends a
        # separate response packet per channel command (3 packets for 3 channels),
        # so one recv() leaves 2 responses in the socket buffer. Those stale
        # responses accumulate and are read by the NEXT drain, making it appear
        # that channels are still in the old state. The loop continues reading
        # until the buffer is truly empty, preventing accumulation entirely.
        self._sock.settimeout(RECV_TIMEOUT_S)
        buf = b""
        try:
            while True:
                chunk = self._sock.recv(RECV_BUF)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        finally:
            self._sock.settimeout(self._connect_timeout)
        if buf:
            log.debug("lighting.response", resp=buf.decode("ascii", errors="replace").strip())
