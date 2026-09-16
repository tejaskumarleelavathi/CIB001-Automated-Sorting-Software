"""
Real lighting controller driver — Advanced Illumination DCS-100E.

Protocol confirmed from hardware_tests/test_lighting.py:
    Commands are semicolon-terminated ASCII, multiple commands batched in one send.
    Three channels (1, 2, 3). set_backlight() treats all three as one on/off
    unit (the normal case — overhead capture wants full brightness).
    set_backlight_channels() (added 2026-08-21) controls a subset
    independently — used for a reduced-brightness EoA capture.

    SET:MODE:CHANNELx,1      → turn channel x ON
    SET:MODE:CHANNELx,0      → turn channel x OFF
    SET:LEVEL:CHANNELx,{mA} → set channel x current in milliamps
    DISCONNECT               → clean close

set_backlight() (2026-08-31, real-hardware finding): switched from 3
sequential SET:MODE:CHANNELx commands to a single LOAD:PROFILE,n command.
Even batched into one TCP write, the 3 SET:MODE commands were visibly
staggering (each applied as the device finished parsing it, not all at
once) -- confirmed against the real DCS-100E/103E manual (which documents
no simultaneous-multi-channel command, only per-channel SET:MODE) and
against hardware_tests/test_lighting_profiles.py's own timing data
(profile switch: lower average AND much lower worst-case latency). A
profile is a config record already saved on the device (both channels'
mode+current together, slots 0-5); loading it is ONE command instead of
three, so there's nothing left for the device to apply sequentially.
Requires the one-time hardware_tests/test_lighting_profiles.py setup
step to have saved slot BACKLIGHT_PROFILE_ON (all channels on) and slot
BACKLIGHT_PROFILE_OFF (all channels off) on this physical unit.
connect() calls _verify_backlight_profiles_sync() once, patiently, to
confirm LOAD:PROFILE genuinely works before trusting it for the rest of
the session (self._profiles_confirmed) -- found live 2026-08-31 that a
per-call check trying to catch failure via response text was NOT enough:
this device's responses can simply not arrive at all within a normal
drain window, and treating silence as success left the backlight dark on
the very first call of a session with no warning at all. If the one-time
check finds the profile isn't configured (e.g. a replacement DCS-100E
swapped in without setup having been re-run), every set_backlight() call
this session falls back to the old, always-reliable per-channel
commands instead. set_backlight_channels() (partial/subset control, used
only for the reduced-brightness EoA frame) is unaffected by any of this
-- it's inherently parameterized per call, which a small fixed set of
saved profiles can't represent.

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
# 0.01 -> 0.05 (2026-08-31, real-hardware finding): the DCS-100E's 3
# per-channel acks don't always land inside a 10ms window -- confirmed live
# from a drained response containing 2 lines from one SET:MODE batch and 2
# from the NEXT one, each missing one channel's confirmation (the straggler
# arrived after the drain gave up and got swept into the following call's
# drain instead, silently). hardware_tests/test_lighting_profiles.py had
# already found 30-50ms necessary for this same class of batched command --
# the production driver's 10ms predates that finding.
RECV_TIMEOUT_S = 0.05
RECV_BUF       = 4096
SEND_RETRIES   = 2             # resend once if not all channels confirmed --
                                # SET:MODE is idempotent, so a retry is safe

# Profile slots used by set_backlight() (2026-08-31) -- must match what
# hardware_tests/test_lighting_profiles.py setup saved on this physical
# DCS-100E. Confirmed via *CHANNEL:CONFIGS? after setup: slot
# BACKLIGHT_PROFILE_ON = all 3 channels mode=1 (continuous) at
# current_ma; slot BACKLIGHT_PROFILE_OFF = all 3 channels mode=0.
BACKLIGHT_PROFILE_ON  = 1
BACKLIGHT_PROFILE_OFF = 2


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
        # Per-channel state — the source of truth. _backlight_on above is
        # derived from it (True only when every channel in CHANNELS is on)
        # so set_backlight()'s "already in that state, skip" optimization
        # stays correct even after set_backlight_channels() calls have
        # changed individual channels independently (2026-08-21).
        self._channels_on: set[int] = set()
        # Set once at connect() by _verify_backlight_profiles_sync() --
        # whether LOAD:PROFILE genuinely works on THIS physical unit
        # (2026-08-31, real-hardware finding: a per-call silent-failure
        # check was not good enough -- the very first set_backlight(True)
        # of a session got zero response within the drain window and was
        # wrongly treated as success, leaving the backlight dark with no
        # warning at all). False (the always-reliable per-channel path)
        # until proven otherwise.
        self._profiles_confirmed = False

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
                # Settle + extra flush before anything else touches this
                # socket (2026-09-16, real-hardware finding: reported live,
                # "this usually happens when the light was already turned
                # on"). If a channel was genuinely ON, turning it off is
                # real device-side work -- its acknowledgment can straggle
                # in well past _flush_stale()'s normal 20ms drain window
                # (see that method's own docstring for the general version
                # of this race). Landing during _verify_backlight_profiles_
                # sync()'s LOAD:PROFILE read right after this loop, it reads
                # as neither "loaded" nor "not set" -- profiles get marked
                # unconfirmed for the whole session, and every backlight
                # toggle afterward falls back to the slow, visibly-
                # staggering per-channel path this was meant to avoid.
                # Already-off channels ack near-instantly and never hit
                # this window, which is exactly why the bug was
                # intermittent and state-dependent rather than constant.
                time.sleep(0.3)
                self._flush_stale()
                self._backlight_on = False
                self._channels_on = set()
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
        self._verify_backlight_profiles_sync()

    def _verify_backlight_profiles_sync(self) -> None:
        """
        One-time, patient check (2026-08-31, real-hardware finding) that
        BACKLIGHT_PROFILE_ON is actually configured on this physical unit,
        before trusting LOAD:PROFILE for every backlight toggle this
        session. Doing this once here, with a generous timeout, means
        every actual runtime set_backlight() call can just fire the
        command and move on -- either fast via profiles (once trust is
        established) or via the always-reliable per-channel path, never a
        per-call guess about whether a missing response means success or
        failure.

        On a CONFIRMED success, leaves the light ON and records that in
        self._backlight_on/_channels_on (2026-09-14 fix -- this check
        loads PROFILE_ON to test it, which genuinely turns the light on;
        turning it back off here unconditionally, only for _startup() to
        call set_backlight(True) a few lines later regardless, produced a
        visible on-off-on blip at every launch with no purpose -- reported
        live, "why can't we just turn it on once and let it stay." The
        immediate set_backlight(True) call now sees _backlight_on already
        True and no-ops (see its "already in that state" guard), so the
        light only ever transitions once: off -> on. Only turned back off
        in the not-confirmed paths below, where the physical state after a
        failed/inconclusive LOAD:PROFILE genuinely is uncertain and the
        conservative choice still applies.
        """
        self._profiles_confirmed = False
        try:
            resp = self._send_batch([f"LOAD:PROFILE,{BACKLIGHT_PROFILE_ON}"])
            if not resp:
                resp = self._poll_for_response(timeout_s=2.0)
            if "not set" in resp.lower():
                log.warning(
                    "lighting.backlight_profiles_not_configured",
                    note=f"Profile {BACKLIGHT_PROFILE_ON} is not set on this "
                         "DCS-100E — run hardware_tests/test_lighting_profiles.py "
                         "setup once. Using slower per-channel commands for "
                         "backlight this session.",
                )
                for ch in CHANNELS:
                    self._send_command(f"SET:MODE:CHANNEL{ch},0")
                self._backlight_on = False
                self._channels_on = set()
            elif "loaded" in resp.lower():
                self._profiles_confirmed = True
                log.info("lighting.backlight_profiles_confirmed")
                # PROFILE_ON just genuinely loaded -- the light IS on right
                # now. Record that instead of turning it back off.
                self._backlight_on = True
                self._channels_on = set(CHANNELS)
            else:
                log.warning(
                    "lighting.backlight_profile_check_inconclusive", resp=resp,
                    note="No clear confirmation from LOAD:PROFILE even after "
                         "waiting — using slower per-channel commands for "
                         "backlight this session.",
                )
                for ch in CHANNELS:
                    self._send_command(f"SET:MODE:CHANNEL{ch},0")
                self._backlight_on = False
                self._channels_on = set()
        except Exception as exc:
            log.warning("lighting.backlight_profile_check_failed", error=str(exc))
            for ch in CHANNELS:
                self._send_command(f"SET:MODE:CHANNEL{ch},0")
            self._backlight_on = False
            self._channels_on = set()

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
        self._channels_on = set()
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

        if self._profiles_confirmed:
            # Single atomic command instead of 3 sequential
            # SET:MODE:CHANNELx (2026-08-31 — see module docstring).
            slot = BACKLIGHT_PROFILE_ON if on else BACKLIGHT_PROFILE_OFF
            self._send_batch([f"LOAD:PROFILE,{slot}"])
            if on:
                # Verify for real on ON specifically (2026-08-31,
                # real-hardware finding): TCP only guarantees the bytes
                # reached the device -- it does NOT guarantee the device's
                # own command parser actually executed them. Confirmed
                # live: the connect-time check succeeding once does not
                # mean every later send also lands -- a send can succeed
                # at the socket level with zero error while the device
                # silently never applies it, and without this check
                # self._backlight_on would still get set True, permanently
                # hiding the failure (the very next ON call would see
                # "already on" and skip sending anything at all). OFF
                # isn't verified -- a delayed/failed OFF just means
                # slightly more stray light for one frame, not a missed
                # detection, so it stays fast/fire-and-forget.
                if not self._verify_backlight_on_sync():
                    log.warning(
                        "lighting.backlight_profile_on_not_confirmed",
                        note="LOAD:PROFILE ON not confirmed via *CHANNEL:CONFIGS? -- "
                             "falling back to per-channel commands for this call.",
                    )
                    confirmed = self._send_batch_verified(
                        [f"SET:MODE:CHANNEL{ch},1" for ch in CHANNELS], expected=len(CHANNELS),
                    )
                    if not confirmed:
                        log.warning(
                            "lighting.backlight_not_fully_confirmed", on=on,
                            note="Not all 3 channels confirmed even after a retry — "
                                 "physical state may not match. Check backlight "
                                 "visually if this repeats.",
                        )
        else:
            mode = 1 if on else 0
            confirmed = self._send_batch_verified(
                [f"SET:MODE:CHANNEL{ch},{mode}" for ch in CHANNELS], expected=len(CHANNELS),
            )
            if not confirmed:
                log.warning(
                    "lighting.backlight_not_fully_confirmed", on=on,
                    note="Not all 3 channels confirmed even after a retry — physical "
                         "state may not match. Check backlight visually if this repeats.",
                )

        self._backlight_on = on
        self._channels_on = set(CHANNELS) if on else set()
        log.debug("lighting.backlight", on=on,
                  method="profile" if self._profiles_confirmed else "per_channel")

    def _verify_backlight_on_sync(self, timeout_s: float = 1.5) -> bool:
        """
        Read back *CHANNEL:CONFIGS? and confirm at least one channel
        actually reports mode="1" -- real proof the device applied the
        command, not just that we sent it. Polls up to timeout_s if the
        response doesn't arrive immediately (this device's replies can lag).
        """
        resp = self._send_batch(["*CHANNEL:CONFIGS?"])
        if 'mode="1"' not in resp:
            resp = self._poll_for_response(timeout_s=timeout_s)
        return 'mode="1"' in resp

    async def set_ring_light(self, on: bool) -> None:
        """Ring light is robot digital output 12 — not controlled by DCS-100E."""
        log.debug("lighting.ring_light_noop", on=on,
                  note="ring light via robot digital output 12")

    async def set_backlight_channels(self, channels: list[int], on: bool) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_backlight_channels_sync, channels, on)

    def _set_backlight_channels_sync(self, channels: list[int], on: bool) -> None:
        """
        Turn specific channels on/off, leaving the rest untouched. Unlike
        _set_backlight_sync, always sends (no "already in that state, skip"
        check) — partial-channel calls are infrequent enough that the extra
        round trip doesn't matter, and skipping here would be harder to
        reason about correctly than just always sending.
        """
        mode = 1 if on else 0
        confirmed = self._send_batch_verified(
            [f"SET:MODE:CHANNEL{ch},{mode}" for ch in channels], expected=len(channels),
        )
        if not confirmed:
            log.warning(
                "lighting.backlight_channels_not_fully_confirmed", channels=channels, on=on,
                note="Not all requested channels confirmed even after a retry.",
            )
        if on:
            self._channels_on.update(channels)
        else:
            self._channels_on.difference_update(channels)
        self._backlight_on = (self._channels_on == set(CHANNELS))
        log.debug("lighting.backlight_channels", channels=channels, on=on,
                  all_channels_on=self._backlight_on)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _send_command(self, command: str) -> None:
        """Send a single command and drain its response (fire-and-forget)."""
        self._send_batch([command])

    def _send_batch_verified(self, commands: list[str], expected: int) -> bool:
        """
        Send `commands` and count how many "set to mode" confirmations come
        back; if fewer than `expected`, resend once more before giving up
        (2026-08-31, real-hardware finding — see RECV_TIMEOUT_S's comment).
        SET:MODE is idempotent for a channel already in the requested state,
        so a full resend is always safe, never a partial/targeted retry.
        Returns True only once `expected` confirmations were actually seen.
        """
        for attempt in range(SEND_RETRIES):
            resp = self._send_batch(commands)
            if resp.count("set to mode") >= expected:
                return True
            if attempt < SEND_RETRIES - 1:
                log.debug(
                    "lighting.send_batch_retry", attempt=attempt + 1,
                    confirmations=resp.count("set to mode"), expected=expected,
                )
        return False

    def _flush_stale(self) -> None:
        """
        Drain and discard anything already sitting in the socket buffer
        BEFORE sending a new command (2026-08-31, real-hardware finding —
        see hardware_tests/test_lighting_profiles.py's identical fix).
        Responses can straggle in well behind their own call's drain
        window; without this, a later call's drain can silently consume a
        prior call's leftover response instead of (or mixed in with) its
        own, making _send_batch_verified's confirmation-counting and
        _set_backlight_sync's "not set" profile check both unreliable.
        """
        if self._sock is None:
            return
        self._sock.settimeout(0.02)
        try:
            while True:
                chunk = self._sock.recv(RECV_BUF)
                if not chunk:
                    break
        except socket.timeout:
            pass
        except OSError:
            pass
        finally:
            self._sock.settimeout(self._connect_timeout)

    def _poll_for_response(self, timeout_s: float) -> str:
        """
        Passively drain (no send) until something arrives or timeout_s
        elapses -- used only by _verify_backlight_profiles_sync()'s
        one-time, patient check, for a command whose response didn't show
        up within the normal short drain window. Not used on the
        per-toggle hot path (see _set_backlight_sync).
        """
        if self._sock is None:
            return ""
        deadline = time.monotonic() + timeout_s
        collected = b""
        while time.monotonic() < deadline:
            self._sock.settimeout(0.1)
            try:
                chunk = self._sock.recv(RECV_BUF)
                if chunk:
                    collected += chunk
                    continue   # keep draining while data is still flowing
            except socket.timeout:
                pass
            except OSError:
                break
            if collected:
                break
        self._sock.settimeout(self._connect_timeout)
        text = collected.decode("ascii", errors="replace").strip()
        if text:
            log.debug("lighting.response", resp=text)
        return text

    def _send_batch(self, commands: list[str]) -> str:
        """
        Send multiple commands in one TCP write, each semicolon-terminated.
        Drain any response with a short timeout — response is not required.
        Returns the decoded response text (empty string if nothing arrived).
        """
        if self._sock is None:
            raise RuntimeError("Lighting controller not connected")

        self._flush_stale()
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
        if not buf:
            return ""
        text = buf.decode("ascii", errors="replace").strip()
        log.debug("lighting.response", resp=text)
        return text
