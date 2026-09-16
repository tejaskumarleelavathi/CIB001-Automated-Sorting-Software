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
# 0.05 -> 1.0 (2026-09-16, real-hardware finding): still not enough. A
# real ack (genuinely turning a channel that was ON off) was confirmed
# straggling in 700ms+ after its own command -- most visibly at connect
# time (see _connect_sync()'s per-channel-off loop, whose straggler then
# corrupted _verify_backlight_profiles_sync()'s LOAD:PROFILE read right
# after it, permanently falling back to the slow per-channel path for
# the whole session), but the same slow-ack pattern also starved
# set_backlight(False)'s own confirmation at shutdown -- reported live,
# the light was still visibly on after "shutdown" completed.
# 1.0 -> 2.5 (2026-09-16, same day, follow-up real-hardware finding):
# 1.0 still wasn't enough margin at connect time. A full connect-time
# log was traced command-by-command and showed EVERY response -- not
# just ones from a genuine on->off state change, "already off" trivial
# acks too -- lands ~1.2-1.3s after its own command, consistently.
#
# 2.5 -> 1.5, same day: tried to split the difference, but the real
# problem wasn't the exact number -- it was architectural. _send_batch()'s
# drain loop always blocks for the FULL RECV_TIMEOUT_S on every call, even
# when the response arrives instantly (it only exits via a read timeout,
# never early), and the per-cycle backlight path was routing every single
# toggle through it plus a read-back verify + retry. So every regular
# set_backlight() call during a run was paying this constant at least
# once, sometimes 2-4 times (LOAD:PROFILE + verify read + retry). No
# single value of RECV_TIMEOUT_S fixes that -- confirmed live again, a
# mid-run cycle's eoa_capture_no_backlight step still ballooned from ~5s
# to 11s+ this same day.
#
# Fix (2026-09-16, operator instruction: "only when we start the system
# does it need to check/flush -- I don't want it waiting every time"):
# removed verification and retry from the hot path entirely.
# _set_backlight_sync/_set_backlight_channels_sync now use
# _send_fire_and_forget() -- one TCP write, no read-back, no wait -- for
# every regular per-cycle call. RECV_TIMEOUT_S (and the old
# _send_batch_verified retry loop it belonged to, now removed along with
# SEND_RETRIES) is used ONLY by the one-time connect() sequence
# (_verify_backlight_off_sync, _verify_backlight_profiles_sync) where a
# multi-second check is a cost worth paying once per session, never per
# cycle.
RECV_TIMEOUT_S = 1.5
RECV_BUF       = 4096

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
                # Set current and turn OFF each channel -- ONE batched send
                # for all 6 commands, not 6 separate sequential round trips
                # (2026-09-16, real-hardware finding: 6 sequential
                # _send_command() calls, each waiting up to RECV_TIMEOUT_S
                # on its own, made the whole lighting connect sequence take
                # 70+ seconds with the larger timeout -- long enough that
                # the operator, seeing neither light turn on with no
                # progress feedback, assumed the system had hung and hit
                # SHUTDOWN mid-startup, which then raced the still-running
                # connect sequence into a real crash). One batched
                # _send_batch() call drains all the responses together in
                # a single wait instead of paying the timeout once per
                # command -- correctness is unaffected, since
                # _verify_backlight_off_sync() right after this is what
                # actually confirms the result either way.
                self._send_batch(
                    [f"SET:LEVEL:CHANNEL{ch},{self._current_ma}" for ch in CHANNELS]
                    + [f"SET:MODE:CHANNEL{ch},0" for ch in CHANNELS]
                )
                # Actively confirm truly off before anything else touches
                # this socket (2026-09-16, operator request: "if it's
                # already on, turn it off, flush/reset, THEN turn on -- if
                # it's already off, just reset and turn on"). Whatever
                # state the device was left in from a previous session
                # (crash, or the operator's own prior on/off toggling),
                # connect() now always starts from a verified-clean OFF
                # instead of assuming the fire-and-forget batch above landed.
                if not self._verify_backlight_off_sync():
                    log.warning(
                        "lighting.connect_off_not_confirmed",
                        note="Channel-off not confirmed via *CHANNEL:CONFIGS? after "
                             "connect -- retrying once before continuing.",
                    )
                    self._send_batch([f"SET:MODE:CHANNEL{ch},0" for ch in CHANNELS])
                    if not self._verify_backlight_off_sync():
                        log.warning(
                            "lighting.connect_off_not_confirmed_after_retry",
                            note="Still not confirmed off -- proceeding anyway; "
                                 "check backlight visually if this repeats.",
                        )
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
        """
        Per-cycle hot path (2026-09-16, operator instruction: "only when we
        start the system does it need to check/flush -- I don't want it
        waiting every time"). Fire-and-forget: send the command and trust
        it, no read-back confirmation, no retry. Confirmation only happens
        once, at connect() time (_verify_backlight_off_sync /
        _verify_backlight_profiles_sync) -- everything after that is
        assumed good until the next connect(). This trades per-call
        certainty for speed on the path that runs every cycle; the
        one-time connect check is what used to (and still does) catch a
        genuinely misbehaving unit.
        """
        if on == self._backlight_on:
            return  # already in the right state — no send, no wait

        if self._profiles_confirmed:
            # Single atomic command instead of 3 sequential
            # SET:MODE:CHANNELx (2026-08-31 — see module docstring).
            slot = BACKLIGHT_PROFILE_ON if on else BACKLIGHT_PROFILE_OFF
            self._send_fire_and_forget([f"LOAD:PROFILE,{slot}"])
        else:
            mode = 1 if on else 0
            self._send_fire_and_forget(
                [f"SET:MODE:CHANNEL{ch},{mode}" for ch in CHANNELS]
            )

        self._backlight_on = on
        self._channels_on = set(CHANNELS) if on else set()
        log.debug("lighting.backlight", on=on,
                  method="profile" if self._profiles_confirmed else "per_channel")

    def _verify_backlight_off_sync(self, timeout_s: float = 2.5) -> bool:
        """
        Read back *CHANNEL:CONFIGS? and confirm NO channel reports
        mode="1". Connect-time only (2026-09-16, operator request: always
        verify a genuinely clean off before ever turning on, regardless
        of what state the device was left in) rather than assuming the
        fire-and-forget per-channel-off loop landed.
        """
        resp = self._send_batch(["*CHANNEL:CONFIGS?"])
        if not resp:
            resp = self._poll_for_response(timeout_s=timeout_s)
        return 'mode="1"' not in resp

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
        reason about correctly than just always sending. Fire-and-forget,
        same reasoning as _set_backlight_sync (2026-09-16) — this also runs
        every cycle (reduced-brightness EoA frame), not just at startup.
        """
        mode = 1 if on else 0
        self._send_fire_and_forget(
            [f"SET:MODE:CHANNEL{ch},{mode}" for ch in channels]
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

    def _send_fire_and_forget(self, commands: list[str]) -> None:
        """
        Send `commands` in one TCP write and return immediately -- no wait
        for a response at all (2026-09-16, operator instruction: don't wait
        during a running cycle, only at startup). Whatever the device
        eventually replies with sits in the socket buffer until the next
        call's _flush_stale() drains and discards it. This is the per-cycle
        hot path (_set_backlight_sync, _set_backlight_channels_sync); it
        relies on connect()'s one-time verified flush/check to have already
        proven the device is responding correctly, so nothing here re-pays
        that cost on every toggle.
        """
        if self._sock is None:
            raise RuntimeError("Lighting controller not connected")
        self._flush_stale()
        payload = "".join(
            cmd if cmd.endswith(";") else cmd + ";"
            for cmd in commands
        )
        self._sock.sendall(payload.encode("ascii"))

    def _flush_stale(self) -> None:
        """
        Drain and discard anything already sitting in the socket buffer
        BEFORE sending a new command (2026-08-31, real-hardware finding —
        see hardware_tests/test_lighting_profiles.py's identical fix).
        Responses can straggle in well behind their own call's drain
        window; without this, a later call's drain can silently consume a
        prior call's leftover response instead of (or mixed in with) its
        own, making connect-time response reads (e.g. the "not set"
        profile check) unreliable.
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
