"""
test_lighting_profiles.py — DCS-100E profile-based instant switching test

The DCS-100E has 6 profile slots (0–5). A profile stores all channel settings
(mode + level for all 3 channels) atomically. LOAD:PROFILE; applies the active
profile in one shot — potentially switching all 3 channels simultaneously
rather than via 3 sequential SET:MODE commands.

The manual documents SAVE:PROFILE,n and LOAD:PROFILE but does NOT print the
command to SELECT which profile slot to load. This test probes several likely
syntaxes until one works, then runs a timing comparison.

Usage:
    conda run -n cib_env python test_lighting_profiles.py setup
        — Save ON settings to slot 1, OFF settings to slot 2.
          Run this ONCE before the other commands.

    conda run -n cib_env python test_lighting_profiles.py probe
        — Probe for the profile-select command syntax. Tries several candidates
          and checks *PROFILE:NUMBER? after each. Run after setup.

    conda run -n cib_env python test_lighting_profiles.py toggle
        — Toggle ON/OFF 10 times via profile, measuring wall-clock time per switch.
          Compare manually against the batch-command approach.

    conda run -n cib_env python test_lighting_profiles.py compare
        — Side-by-side timing: 10 profile switches vs 10 batch-SET:MODE switches.
          Best run in a dim room so you can see the light physically.

    conda run -n cib_env python test_lighting_profiles.py single
        — One slow ON transition per method, each with a 5s countdown and a
          long hold, meant for slow-motion phone video (2026-08-31 —
          alternating toggles are too fast for the naked eye to judge
          simultaneity on). Record through each transition, then play back
          frame by frame: does Method A (profile) show all 3 channels
          brightening in the same frame, while Method B (batch SET:MODE)
          shows them brightening across separate frames?

    conda run -n cib_env python test_lighting_profiles.py on
    conda run -n cib_env python test_lighting_profiles.py off
        — Quick manual on/off via profile (after setup + probe have succeeded).

Device:  Advanced Illumination DCS-100E
IP:      172.16.9.20, port 777
"""

import socket
import time
import sys

DCS_IP     = "172.16.9.20"
DCS_PORT   = 777
TIMEOUT    = 3.0
CHANNELS   = [1, 2, 3]
CURRENT_MA = 250

# Profile slot assignments
PROFILE_ON  = 1   # all channels at CURRENT_MA, mode=continuous
PROFILE_OFF = 2   # all channels mode=off

# Confirmed working 2026-08-31 via 'probe' (twice, against both slot 1 and
# slot 2) -- LOAD:PROFILE,n selects AND applies a profile in one atomic
# command, matching the real DCS-100E/103E manual's own command table.
# Hardcoded here rather than re-discovered by toggle/compare/on/off's own
# quick inline check every run -- that quick check uses a much less patient
# drain than 'probe' does and this device's response timing has proven
# unpredictable run to run (sometimes near-instant, sometimes very slow),
# so a single fast re-probe attempt is not a reliable way to re-verify
# something already established.
CONFIRMED_SELECT_TEMPLATE = "LOAD:PROFILE,%d"

# Will be populated by probe() — the working profile-select command template.
# %d is replaced with the slot number.
_SELECT_CMD: str | None = None


# ---------------------------------------------------------------------------
# Low-level socket helpers
# ---------------------------------------------------------------------------

def connect() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect((DCS_IP, DCS_PORT))
    except OSError as e:
        print(f"[ERROR] Cannot connect to {DCS_IP}:{DCS_PORT} — {e}")
        sys.exit(1)
    return sock


def _flush_stale(sock: socket.socket) -> str:
    """
    Drain and discard anything already sitting in the socket buffer BEFORE
    sending a new command (2026-08-31, real-hardware finding): responses
    can straggle in well behind their own call's drain window (confirmed
    live -- a *CHANNEL:CONFIGS? XML reply showed up FOUR commands later,
    and a "Command not found" error for one candidate showed up attached
    to a totally different, later candidate). Without this, every call's
    drain can silently consume a PRIOR call's leftover response instead of
    (or mixed in with) its own, making every result downstream suspect.
    Returns whatever stale bytes were found (for diagnostic printing).
    """
    sock.settimeout(0.02)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    finally:
        sock.settimeout(TIMEOUT)
    if buf:
        text = buf.decode("ascii", errors="replace").strip()
        print(f"  !! flushed stale bytes (leftover from an earlier call): {text!r}")
    return buf.decode("ascii", errors="replace").strip() if buf else ""


def send_batch(sock: socket.socket, commands: list[str], drain_ms: float = 400) -> str:
    """
    Send all commands in one TCP write, drain responses for drain_ms ms.
    50 -> 400 default (2026-08-31, real-hardware finding): this device's
    replies can lag far past a 50-100ms window for some commands
    (especially *CHANNEL:CONFIGS? and anything touching EEPROM like
    SAVE:PROFILE) -- confirmed live via responses arriving multiple calls
    late. Always flushes any stale leftover bytes from a PRIOR call first,
    so this call's drain can't be contaminated by (or mistaken for) a
    response that was never actually ours.
    """
    _flush_stale(sock)

    payload = "".join(cmd if cmd.endswith(";") else cmd + ";" for cmd in commands)
    print(f"  >> {payload}")
    sock.sendall(payload.encode("ascii"))

    # Drain all response packets until silence for drain_ms.
    sock.settimeout(drain_ms / 1000.0)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    finally:
        sock.settimeout(TIMEOUT)

    resp = buf.decode("ascii", errors="replace").strip()
    if resp:
        print(f"  << {resp}")
    else:
        print(f"  << (no response within {drain_ms:.0f}ms)")
    return resp


def send(sock: socket.socket, cmd: str, drain_ms: float = 400) -> str:
    return send_batch(sock, [cmd], drain_ms=drain_ms)


def wait_for_response(sock: socket.socket, contains: str, timeout_s: float = 5.0) -> str:
    """
    Keep draining (in short slices) until a response containing `contains`
    shows up, or timeout_s elapses. For commands whose confirmation text is
    known (2026-08-31 — per the real DCS-100E/103E manual, SAVE:PROFILE,n
    should return exactly 'INFO: Profile "n" has been saved.') this catches
    it even if it arrives well past a single fixed drain window, rather
    than accepting whatever unrelated stale text a short window happened
    to catch instead.
    """
    deadline = time.perf_counter() + timeout_s
    collected = ""
    while time.perf_counter() < deadline:
        chunk = _flush_stale(sock)   # non-blocking-ish (20ms), reusable as a poll
        if chunk:
            collected += chunk
            if contains.lower() in collected.lower():
                return collected
        time.sleep(0.1)
    return collected


def query(sock: socket.socket, cmd: str) -> str:
    """Send a query command and return the response (longer drain for XML replies)."""
    return send_batch(sock, [cmd], drain_ms=800)


# ---------------------------------------------------------------------------
# Channel state helpers
# ---------------------------------------------------------------------------

def set_all_on(sock: socket.socket) -> None:
    """Set all 3 channels to continuous mode at CURRENT_MA."""
    cmds = []
    for ch in CHANNELS:
        cmds.append(f"SET:LEVEL:CHANNEL{ch},{CURRENT_MA}")
        cmds.append(f"SET:MODE:CHANNEL{ch},1")
    send_batch(sock, cmds)


def set_all_off(sock: socket.socket) -> None:
    """Turn all 3 channels off."""
    cmds = [f"SET:MODE:CHANNEL{ch},0" for ch in CHANNELS]
    send_batch(sock, cmds)


# ---------------------------------------------------------------------------
# SETUP — save profiles 1 (ON) and 2 (OFF) to EEPROM
# ---------------------------------------------------------------------------

def cmd_setup(sock: socket.socket) -> None:
    """
    Save backlight-ON settings to profile slot 1 and OFF to slot 2.
    Run this once — settings survive power cycles.

    2026-08-31, real-hardware finding: dropped the cosmetic
    SET:PROFILE:NAME step entirely -- it was consistently the single
    slowest command on this device (its response was seen arriving
    multiple ENTIRE STEPS late, once even after DISCONNECT had already
    been sent), and every command queued up behind it inherited that
    delay. It isn't needed for LOAD:PROFILE,n to work -- selection is by
    numeric slot, not name. Replaced blind "saved" print statements with
    real verification: after saving, LOAD each profile back and read
    *CHANNEL:CONFIGS? to confirm the actual channel mode/current values
    match what was intended, instead of trusting the SAVE response alone
    (which the previous run showed can claim success while the content
    still doesn't stick -- LOAD:PROFILE,1 kept saying "not set" even
    after "Profile 1 (BKLT_ON) saved." had printed).
    """
    print("=" * 60)
    print("STEP 1: Query current device state")
    print("=" * 60)
    query(sock, "*IDN?")
    query(sock, "*CHANNEL:CONFIGS?")
    query(sock, "*PROFILE:NUMBER?")

    print()
    print("=" * 60)
    print(f"STEP 2: Set all channels ON at {CURRENT_MA} mA → save as profile {PROFILE_ON}")
    print("=" * 60)
    set_all_on(sock)
    time.sleep(1.0)   # let the device fully settle before saving
    send(sock, f"SAVE:PROFILE,{PROFILE_ON}")
    # Per the real DCS-100E/103E manual, this should return exactly
    # 'INFO: Profile "n" has been saved.' — wait specifically for that
    # instead of trusting whatever the fixed drain window happened to
    # catch (which last run was a stale, unrelated response).
    resp = wait_for_response(sock, "has been saved")
    print(f"  SAVE:PROFILE,{PROFILE_ON} confirmation: "
          f"{'CONFIRMED' if 'has been saved' in resp.lower() else 'NOT SEEN — ' + repr(resp)}")

    print()
    print("=" * 60)
    print(f"STEP 3: Set all channels OFF → save as profile {PROFILE_OFF}")
    print("=" * 60)
    set_all_off(sock)
    time.sleep(1.0)
    send(sock, f"SAVE:PROFILE,{PROFILE_OFF}")
    resp = wait_for_response(sock, "has been saved")
    print(f"  SAVE:PROFILE,{PROFILE_OFF} confirmation: "
          f"{'CONFIRMED' if 'has been saved' in resp.lower() else 'NOT SEEN — ' + repr(resp)}")

    print()
    print("=" * 60)
    print("STEP 4: Verify by loading each profile back and reading channel config")
    print("=" * 60)
    for slot, label in ((PROFILE_ON, "ON"), (PROFILE_OFF, "OFF")):
        send(sock, f"LOAD:PROFILE,{slot}")
        time.sleep(0.5)
        resp = query(sock, "*CHANNEL:CONFIGS?")
        if f'profile="{slot}"' not in resp:
            # Query's own 800ms drain didn't catch it -- keep passively
            # polling for a straggler rather than assuming it never came.
            extra = wait_for_response(sock, f'profile="{slot}"', timeout_s=3.0)
            if extra:
                resp = extra
        ok = f'profile="{slot}"' in resp and "not set" not in resp.lower()
        print(f"  Profile {slot} ({label}): {'OK — content confirmed' if ok else 'FAILED — check output above'}")
        print(f"    {resp}")
    # Leave the light off when setup finishes.
    send(sock, f"LOAD:PROFILE,{PROFILE_OFF}")

    print()
    print("Setup complete. Run 'probe' next to find the profile-select syntax.")


# ---------------------------------------------------------------------------
# PROBE — discover the profile-select command syntax
# ---------------------------------------------------------------------------

# Candidates to try, in priority order. %d is the profile slot number.
_SELECT_CANDIDATES = [
    "SET:PROFILE,%d",
    "SET:ACTIVE:PROFILE,%d",
    "SELECT:PROFILE,%d",
    "PROFILE:SELECT,%d",
    "SET:PROFILE:NUMBER,%d",
    "LOAD:PROFILE,%d",       # maybe LOAD:PROFILE accepts a slot number directly
    "SET:PROFILE:ACTIVE,%d",
]


def _try_select(sock: socket.socket, template: str, slot: int) -> bool:
    """
    Try one profile-select candidate. Returns True if *PROFILE:NUMBER? confirms
    the active profile changed to `slot` after the command.
    """
    cmd = template % slot
    print(f"\n  Trying: {cmd!r}")
    send(sock, cmd)   # default drain_ms=400, plus a pre-flush of any leftovers
    time.sleep(0.15)  # let the device fully settle before asking it to confirm
    resp = query(sock, "*PROFILE:NUMBER?")
    # Look for the slot number in the response
    if str(slot) in resp:
        print(f"  [MATCH] *PROFILE:NUMBER? returned {resp!r} — '{template}' WORKS!")
        return True
    print(f"  [NO]    *PROFILE:NUMBER? returned {resp!r}")
    return False


def cmd_probe(sock: socket.socket) -> str | None:
    """
    Probe for the working profile-select syntax by trying all candidates.
    Returns the working template string, or None if none worked.
    """
    print("=" * 60)
    print("PROBE: Finding profile-select command syntax")
    print("=" * 60)
    print(f"Target: switch to profile slot {PROFILE_ON}, verify via *PROFILE:NUMBER?")
    print()

    for template in _SELECT_CANDIDATES:
        if _try_select(sock, template, PROFILE_ON):
            # Double-check: can we also select the OFF profile?
            print(f"\n  Verifying with slot {PROFILE_OFF} ...")
            ok2 = _try_select(sock, template, PROFILE_OFF)
            if ok2:
                print(f"\n[SUCCESS] Working select syntax: {template!r}")
                print(f"  Use:  {template % PROFILE_ON}; LOAD:PROFILE;  → backlight ON")
                print(f"  Use:  {template % PROFILE_OFF}; LOAD:PROFILE; → backlight OFF")
                return template
            else:
                print(f"  Second check failed — trying next candidate")

    print()
    print("[FAIL] None of the candidate commands changed *PROFILE:NUMBER?.")
    print("       Possible causes:")
    print("       1. LOAD:PROFILE; does not use a selectable active profile —")
    print("          it may always restore the last-used profile (not useful).")
    print("       2. The profile-select command has a different syntax not tried here.")
    print("       3. Profiles are loaded by slot directly in LOAD:PROFILE,n syntax")
    print("          (already tried above as a candidate).")
    print()
    print("       FALLBACK: Profile approach will not work for this firmware.")
    print("       Best alternative: hardware trigger (gated mode, pin TTL).")
    return None


# ---------------------------------------------------------------------------
# TOGGLE — rapid on/off via profile with timing
# ---------------------------------------------------------------------------

def cmd_toggle(sock: socket.socket, select_template: str, cycles: int = 10) -> None:
    """
    Toggle backlight ON/OFF `cycles` times using profile switch.
    Prints wall-clock time per toggle. Watch the physical light.
    """
    print("=" * 60)
    print(f"TOGGLE TEST: {cycles} on/off cycles via profile switch")
    print("  Watch the backlight — should flash with no visible sequential")
    print("  channel-by-channel brightening.")
    print("=" * 60)
    print()

    times_on  = []
    times_off = []

    for i in range(cycles):
        # --- ON ---
        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_ON], drain_ms=30)
        t1 = time.perf_counter()
        dt_on = (t1 - t0) * 1000
        times_on.append(dt_on)
        print(f"  Cycle {i+1:2d}  ON  → {dt_on:6.1f} ms")
        time.sleep(0.5)   # hold ON so you can see it

        # --- OFF ---
        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_OFF], drain_ms=30)
        t1 = time.perf_counter()
        dt_off = (t1 - t0) * 1000
        times_off.append(dt_off)
        print(f"  Cycle {i+1:2d}  OFF → {dt_off:6.1f} ms")
        time.sleep(0.5)   # hold OFF so you can see it

    print()
    print(f"  ON  avg={sum(times_on)/len(times_on):.1f} ms  "
          f"min={min(times_on):.1f}  max={max(times_on):.1f}")
    print(f"  OFF avg={sum(times_off)/len(times_off):.1f} ms  "
          f"min={min(times_off):.1f}  max={max(times_off):.1f}")


# ---------------------------------------------------------------------------
# COMPARE — profile switch vs batch SET:MODE, side by side
# ---------------------------------------------------------------------------

def cmd_compare(sock: socket.socket, select_template: str, cycles: int = 10) -> None:
    """
    Compare profile-switch timing against 3-command batch SET:MODE timing.
    Runs both methods and prints the results side by side.
    """
    print("=" * 60)
    print(f"COMPARE: profile switch vs batch SET:MODE ({cycles} cycles each)")
    print("=" * 60)

    # ---- Profile method ----
    print("\n[METHOD A] Profile switch")
    profile_on_times  = []
    profile_off_times = []
    for _ in range(cycles):
        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_ON], drain_ms=30)
        profile_on_times.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.3)

        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_OFF], drain_ms=30)
        profile_off_times.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.3)

    # ---- Batch SET:MODE method ----
    print("\n[METHOD B] Batch SET:MODE (3 commands, 1 TCP write)")
    batch_on_times  = []
    batch_off_times = []
    for _ in range(cycles):
        t0 = time.perf_counter()
        send_batch(sock, [f"SET:MODE:CHANNEL{ch},1" for ch in CHANNELS], drain_ms=30)
        batch_on_times.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.3)

        t0 = time.perf_counter()
        send_batch(sock, [f"SET:MODE:CHANNEL{ch},0" for ch in CHANNELS], drain_ms=30)
        batch_off_times.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.3)

    # ---- Summary ----
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"{'Method':<22} {'ON avg':>8} {'ON min':>8} {'ON max':>8}")
    print(f"  Profile switch       {sum(profile_on_times)/len(profile_on_times):>7.1f}ms "
          f"{min(profile_on_times):>7.1f}ms {max(profile_on_times):>7.1f}ms")
    print(f"  Batch SET:MODE       {sum(batch_on_times)/len(batch_on_times):>7.1f}ms "
          f"{min(batch_on_times):>7.1f}ms {max(batch_on_times):>7.1f}ms")
    print()
    print(f"{'Method':<22} {'OFF avg':>8} {'OFF min':>8} {'OFF max':>8}")
    print(f"  Profile switch       {sum(profile_off_times)/len(profile_off_times):>7.1f}ms "
          f"{min(profile_off_times):>7.1f}ms {max(profile_off_times):>7.1f}ms")
    print(f"  Batch SET:MODE       {sum(batch_off_times)/len(batch_off_times):>7.1f}ms "
          f"{min(batch_off_times):>7.1f}ms {max(batch_off_times):>7.1f}ms")
    print()
    print("Also check physically: did channels light up simultaneously (profile)")
    print("vs sequentially (batch)? Timing numbers alone don't capture simultaneity.")


# ---------------------------------------------------------------------------
# SINGLE — one slow transition per method, for slow-motion video comparison
# ---------------------------------------------------------------------------

def cmd_single(sock: socket.socket, select_template: str) -> None:
    """
    One ON transition via profile switch, one ON transition via batch
    SET:MODE, each with a countdown and a long hold -- a rapid toggle
    (compare/toggle) is too fast for the naked eye to judge simultaneity
    on (2026-08-31, operator feedback). Point a phone's slow-motion video
    mode (120-240fps on most phones -- more than enough to catch even a
    tens-of-ms stagger) at the light and record through each transition;
    watching it back frame by frame is the only reliable way to actually
    see a channel-by-channel ripple this fast.
    """
    def countdown(label: str) -> None:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
        print("Start your phone's SLOW-MOTION video recording now, aimed at the light.")
        for n in (5, 4, 3, 2, 1):
            print(f"  {n} ...")
            time.sleep(1.0)
        print("  >>> SWITCHING NOW <<<")

    set_all_off(sock)
    time.sleep(1.0)

    countdown("METHOD A: Profile switch — about to turn ON")
    send_batch(sock, [select_template % PROFILE_ON], drain_ms=200)
    print("  (light should now be ON — stop recording, check the playback)")
    time.sleep(4.0)

    print("\nTurning off, resetting for Method B ...")
    send_batch(sock, [select_template % PROFILE_OFF], drain_ms=200)
    time.sleep(2.0)

    countdown("METHOD B: Batch SET:MODE — about to turn ON")
    send_batch(sock, [f"SET:MODE:CHANNEL{ch},1" for ch in CHANNELS], drain_ms=200)
    print("  (light should now be ON — stop recording, check the playback)")
    time.sleep(4.0)

    set_all_off(sock)
    print("\nDone. Review both recordings frame-by-frame: does Method A show all 3")
    print("channels brightening in the same frame, while Method B shows them")
    print("brightening across 2-3 separate frames?")


# ---------------------------------------------------------------------------
# Quick on/off via profile (after setup + probe)
# ---------------------------------------------------------------------------

def cmd_on(sock: socket.socket, select_template: str) -> None:
    print("Profile ON ...")
    send_batch(sock, [select_template % PROFILE_ON], drain_ms=50)
    print("Done.")


def cmd_off(sock: socket.socket, select_template: str) -> None:
    print("Profile OFF ...")
    send_batch(sock, [select_template % PROFILE_OFF], drain_ms=50)
    print("Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "help"

    if cmd == "help":
        print(__doc__)
        sys.exit(0)

    sock = connect()
    print(f"Connected to DCS-100E at {DCS_IP}:{DCS_PORT}")

    with sock:
        if cmd == "setup":
            cmd_setup(sock)

        elif cmd == "probe":
            result = cmd_probe(sock)
            if result:
                print(f"\nWorking template: {result!r}")
                print("Save this — pass it to toggle/compare/on/off via --select flag,")
                print("or hardcode it in the driver once confirmed.")
            # Leave light off after probe
            set_all_off(sock)

        elif cmd in ("toggle", "compare", "single", "on", "off"):
            # Use the already-confirmed syntax directly (2026-08-31) rather
            # than re-discovering it every run -- 'probe' already verified
            # it twice, patiently, against both slot 1 and slot 2. A quick
            # single-attempt re-check here was proving flaky on its own
            # (this device's response timing is unpredictable run to run),
            # which was blocking real tests of an already-confirmed fact.
            found = CONFIRMED_SELECT_TEMPLATE

            if cmd == "toggle":
                cmd_toggle(sock, found)
                set_all_off(sock)   # leave off after test
            elif cmd == "compare":
                cmd_compare(sock, found)
                set_all_off(sock)
            elif cmd == "single":
                cmd_single(sock, found)
            elif cmd == "on":
                cmd_on(sock, found)
            elif cmd == "off":
                cmd_off(sock, found)

        else:
            print(f"Unknown command: {cmd!r}")
            print("Usage: setup | probe | toggle | compare | single | on | off")
            sys.exit(1)

        send(sock, "DISCONNECT")
