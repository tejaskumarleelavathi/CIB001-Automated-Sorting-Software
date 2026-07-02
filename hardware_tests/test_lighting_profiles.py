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


def send_batch(sock: socket.socket, commands: list[str], drain_ms: float = 50) -> str:
    """Send all commands in one TCP write, drain responses for drain_ms ms."""
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
    return resp


def send(sock: socket.socket, cmd: str, drain_ms: float = 50) -> str:
    return send_batch(sock, [cmd], drain_ms=drain_ms)


def query(sock: socket.socket, cmd: str) -> str:
    """Send a query command and return the response (longer drain for XML replies)."""
    return send_batch(sock, [cmd], drain_ms=200)


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
    """
    print("=" * 60)
    print("STEP 1: Query current device state")
    print("=" * 60)
    query(sock, "*IDN?")
    query(sock, "*CHANNEL:CONFIGS?")
    query(sock, "*PROFILE:NUMBER?")
    query(sock, "*PROFILE:NAMES?")

    print()
    print("=" * 60)
    print(f"STEP 2: Set all channels ON at {CURRENT_MA} mA → save as profile {PROFILE_ON}")
    print("=" * 60)
    set_all_on(sock)
    time.sleep(0.1)
    send(sock, f"SET:PROFILE:NAME,BKLT_ON")
    send(sock, f"SAVE:PROFILE,{PROFILE_ON}")
    time.sleep(0.2)   # EEPROM write needs a moment
    print(f"  Profile {PROFILE_ON} (BKLT_ON) saved.")

    print()
    print("=" * 60)
    print(f"STEP 3: Set all channels OFF → save as profile {PROFILE_OFF}")
    print("=" * 60)
    set_all_off(sock)
    time.sleep(0.1)
    send(sock, f"SET:PROFILE:NAME,BKLT_OFF")
    send(sock, f"SAVE:PROFILE,{PROFILE_OFF}")
    time.sleep(0.2)
    print(f"  Profile {PROFILE_OFF} (BKLT_OFF) saved.")

    print()
    print("=" * 60)
    print("STEP 4: Verify profiles saved")
    print("=" * 60)
    query(sock, "*PROFILE:NAMES?")
    query(sock, "*CHANNEL:CONFIGS?")
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
    send(sock, cmd, drain_ms=100)
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
        send_batch(sock, [select_template % PROFILE_ON, "LOAD:PROFILE"], drain_ms=30)
        t1 = time.perf_counter()
        dt_on = (t1 - t0) * 1000
        times_on.append(dt_on)
        print(f"  Cycle {i+1:2d}  ON  → {dt_on:6.1f} ms")
        time.sleep(0.5)   # hold ON so you can see it

        # --- OFF ---
        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_OFF, "LOAD:PROFILE"], drain_ms=30)
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
        send_batch(sock, [select_template % PROFILE_ON, "LOAD:PROFILE"], drain_ms=30)
        profile_on_times.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.3)

        t0 = time.perf_counter()
        send_batch(sock, [select_template % PROFILE_OFF, "LOAD:PROFILE"], drain_ms=30)
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
# Quick on/off via profile (after setup + probe)
# ---------------------------------------------------------------------------

def cmd_on(sock: socket.socket, select_template: str) -> None:
    print("Profile ON ...")
    send_batch(sock, [select_template % PROFILE_ON, "LOAD:PROFILE"], drain_ms=50)
    print("Done.")


def cmd_off(sock: socket.socket, select_template: str) -> None:
    print("Profile OFF ...")
    send_batch(sock, [select_template % PROFILE_OFF, "LOAD:PROFILE"], drain_ms=50)
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

        elif cmd in ("toggle", "compare", "on", "off"):
            # For these commands we need the select template.
            # Try to auto-discover it first (quick probe, no printing).
            print("Auto-probing for working profile-select syntax ...")
            found = None
            for template in _SELECT_CANDIDATES:
                cmd_str = template % PROFILE_ON
                sock.sendall((cmd_str + ";").encode("ascii"))
                time.sleep(0.05)
                sock.settimeout(0.2)
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

                resp_check = query(sock, "*PROFILE:NUMBER?")
                if str(PROFILE_ON) in resp_check:
                    found = template
                    print(f"  Using: {template!r}")
                    break

            if found is None:
                print("[WARN] Could not auto-discover profile-select syntax.")
                print("       Run 'probe' first to identify the working command.")
                print("       Falling back to batch SET:MODE for on/off commands.")
                if cmd == "on":
                    set_all_on(sock)
                elif cmd == "off":
                    set_all_off(sock)
                send(sock, "DISCONNECT")
                sys.exit(0)

            if cmd == "toggle":
                cmd_toggle(sock, found)
                set_all_off(sock)   # leave off after test
            elif cmd == "compare":
                cmd_compare(sock, found)
                set_all_off(sock)
            elif cmd == "on":
                cmd_on(sock, found)
            elif cmd == "off":
                cmd_off(sock, found)

        else:
            print(f"Unknown command: {cmd!r}")
            print("Usage: setup | probe | toggle | compare | on | off")
            sys.exit(1)

        send(sock, "DISCONNECT")
