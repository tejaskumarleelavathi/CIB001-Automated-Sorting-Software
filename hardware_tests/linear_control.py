"""
linear_control.py — Novanta Liberty MDrive Linear Actuator
-----------------------------------------------------------
Device:   Novanta LMDAE572C (linear actuator — external threaded lead screw)
IP:       172.16.9.60
Port:     503  (MCode/TCP — raw ASCII, NOT Modbus, NOT Telnet)

This is a LINEAR actuator, not a rotary motor.  Motion moves a carriage
along a lead screw.  All positions are in raw steps — do NOT assume a
real-world distance until lead screw pitch is confirmed and measured.

Programs stored on motor flash:
  G1  →  MA 0              (drop / dispense — absolute move TO HOME)
  G2  →  MA ASPIRE_STEPS   (aspire / draw — absolute move to a fixed
                            position measured FROM home)
  G3  →  home to the dispense-end hard stop (2026-09-01, see
          upload_programs()'s docstring for the full MCode body)
  G4  →  G3's internal success handler (not meant to be EX'd directly)

Python just sends "EX G1" / "EX G2" / "EX G3" to trigger them.  The motor
executes the move autonomously and signals completion via the ">" prompt.

G1/G2 switched from relative (MR) to absolute (MA) moves 2026-09-07,
once G3's hard-stop homing made P=0 a reliable, physically-verified
reference point (see the ASPIRE_STEPS comment below for the full
reasoning). MR/relative moves are still used for the `move <n>`
diagnostic command below -- that's for ad-hoc testing of an arbitrary
distance from wherever the carriage currently is, a different job from
G1/G2's fixed, self-correcting production positions.

Labels G1 / G2 / G3 / G4 chosen because:
  - G prefix is not a reserved MCode mnemonic in LMDAM/LMDAE firmware
  - Lessons from first motor: F* prefix triggers ER=24 (reserved); A* safe,
    G* also safe — use G here to distinguish from the rotary motor's A* labels

Usage:
  python hardware_tests/linear_control.py upload    # flash G1+G2+G3+G4 to motor (run once)
  python hardware_tests/linear_control.py drop      # execute G1 (absolute move to home)
  python hardware_tests/linear_control.py aspire    # execute G2 (absolute move to ASPIRE_STEPS)
  python hardware_tests/linear_control.py move <n>  # raw MR <n>, any step count, no re-flash
  python hardware_tests/linear_control.py pos       # print position, no motion
  python hardware_tests/linear_control.py home      # home, orchestrated from this script
  python hardware_tests/linear_control.py home2     # home, via the onboard G3 program (EX G3)
  python hardware_tests/linear_control.py run       # interactive loop

Protocol recap:
  - Every command = ASCII + CR (\\r)
  - Immediate mode: response ends with ">" (ok) or "?" (error)
  - Program mode:   response is a bare address number e.g. "  6" (no ">" prompt)
  - "?" in program mode = error — read PR ER immediately and abort cleanly
"""

import socket
import time
import sys

# ── Connection ────────────────────────────────────────────────────────────────
LINEAR_IP    = "172.16.9.60"
LINEAR_PORT  = 503

# ── Motion parameters ───────────────────────────────────────────────────────
# 200,000 steps ran successfully in production — confirmed working baseline.
# Increased +50,000 -> 250,000 -> +100,000 -> 350,000 -> 3x -> 1,050,000
# (all 2026-08-19), then 1.5x -> 1,575,000 same day (operator testing
# different aspirate configs to reduce the pipette tip disturbing/pushing
# the specimen before it can be captured — see DEV-16). Drop increased by
# the same amount every time to stay symmetric.
# 1,575,000 (above) confirmed overdriving on real hardware 2026-08-21
# (optimize-speed pass — "it is over driving"). Many values tried since
# (1,102,500 / 1,350,000 / 1,250,000 / 1,300,000 / 1,540,000 / 1,600,000 /
# 1,000,000) swinging between "driving less" (not enough suction) and
# overdriving, none landing cleanly — see git history on this file for the
# blow-by-blow if it matters later. 800,000 confirmed working and used in
# production through the DEV-11..DEV-16 tuning and the G3 homing work.
#
# 2026-09-07: G1 (drop) and G2 (aspire) switched from relative (MR) to
# absolute (MA) moves, now that G3's hard-stop-plus-backoff homing makes
# P=0 a reliable, physically-verified reference point rather than just
# "wherever the motor's own step counter thinks it is." Under the old MR
# scheme, DROP_STEPS/ASPIRE_STEPS were a MATCHED PAIR of relative
# distances (aspirate pulls back N steps from wherever it is, drop pushes
# forward the same N steps to return) -- any missed step or timing hiccup
# silently accumulated drift forever, which is the whole reason homing
# became necessary in the first place. Under MA, there is no more
# "DROP_STEPS" as a separate tunable distance at all: G1 always targets
# the single fixed absolute position MA 0 (home itself -- by construction
# the correct, safe dispense position, already verified this session to
# sit HOME_BACKOFF_STEPS clear of the true hard stop), and G2 targets a
# single fixed absolute position, ASPIRE_STEPS, measured FROM home. Both
# now self-correct every cycle instead of compounding error between
# cycles -- there is nothing to "stay symmetric" with anymore.
#
# ASPIRE_STEPS raised 800,000 -> 1,000,000 same day (operator's own call,
# "aspirate 1000000 not 800000") alongside this MA conversion.
ASPIRE_STEPS = 1_200_000   # absolute target position, measured from home (P=0)

# Velocity profile (steps/sec, steps/sec^2) — governs how FAST G1/G2 run,
# completely separate from ASPIRE_STEPS above (which only governs
# how FAR). Never set anywhere in this file before 2026-08-19 — motor was
# running at factory/flash default the whole time (VM=768000, VI=1000,
# A=D=1000000). Increased to VM=1,200,000/A=D=2,000,000 same day, then VM
# 2x'd -> 2,400,000 (A/D left unchanged — operator asked to increase
# "speed" specifically, not acceleration) same day, testing configs for the
# tip-disturbance issue (DEV-16). A/D 2x'd -> 4,000,000 on 2026-08-21
# (optimize-speed pass, operator's own call — "update the acceleration
# too").
#
# 2026-08-21, same day: at A=4,000,000 with the current ASPIRE_STEPS/
# DROP_STEPS travel distance (800,000), the ramp-up+ramp-down distance
# needed to reach VM=2,400,000 is 1,440,000 steps — MORE than the travel
# distance, so this move is "triangular": it never reaches VELOCITY_MAX at
# all (peak speed only ~1.79M, calculated stroke time ~0.89s) — VM isn't
# the limiting factor here, ACCEL/DECEL is. A higher-accel profile (A/D
# 10,000,000, VM 4,000,000) was tried and reverted, untested, same day —
# see git history on this file if that's worth revisiting once the step
# count itself is settled.
#
# Steeper acceleration draws more torque from the motor and more resistance
# from the syringe plunger/fluid — watch closely for a stall (ER=86) if
# these values are raised again; back A/D off if it stalls or skips steps.
VELOCITY_MAX =  2_400_000   # VM
ACCEL        =  4_000_000   # A
DECEL        =  4_000_000   # D

# ── Timing ────────────────────────────────────────────────────────────────────
CMD_TIMEOUT_S   = 5.0    # max wait for a single command response
MOVE_TIMEOUT_S  = 60.0   # max wait for a move to complete (linear moves can be slow)
POLL_INTERVAL_S = 0.25   # PR MV poll frequency while moving


# ── Low-level socket helpers ─────────────────────────────────────────────────

def connect() -> socket.socket:
    """Open a raw TCP connection to the linear actuator.  Exits on failure."""
    print(f"[CONNECT] {LINEAR_IP}:{LINEAR_PORT} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(CMD_TIMEOUT_S)
        sock.connect((LINEAR_IP, LINEAR_PORT))
        print("[CONNECT] Connected.\n")
        return sock
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"[ERROR] Cannot connect to {LINEAR_IP}:{LINEAR_PORT} — {exc}")
        sys.exit(1)


def _read_prompt(sock: socket.socket) -> str:
    """
    Read bytes from *sock* until '>' or '?' appears, or CMD_TIMEOUT_S elapses.
    Returns the full accumulated response (stripped).
    Used for IMMEDIATE MODE commands only.
    """
    buf = ""
    deadline = time.monotonic() + CMD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            buf += sock.recv(256).decode("ascii", errors="replace")
            if ">" in buf or "?" in buf:
                break
        except socket.timeout:
            continue
    return buf.strip()


def _read_pgm(sock: socket.socket) -> str:
    """
    Read one response while IN program mode.

    In program mode the motor NEVER sends '>'.  Instead it echoes the command
    then prints the next available program address as a bare number (e.g. "  6").
    An error still produces '?'.

    We stop reading when:
      - A '?' appears anywhere  (error)
      - A line whose stripped content is purely digits  (address counter = ok)
    """
    buf = ""
    deadline = time.monotonic() + CMD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            buf += sock.recv(256).decode("ascii", errors="replace")
            if "?" in buf:
                break
            for line in buf.splitlines():
                if line.strip().isdigit():
                    break
            else:
                continue
            break
        except socket.timeout:
            continue
    return buf.strip()


def send_cmd(sock: socket.socket, command: str, verbose: bool = True) -> tuple[str, bool]:
    """
    Send one command in IMMEDIATE MODE.
    Returns (raw_response, ok) where ok=True means '>' prompt (no error).
    """
    if verbose:
        print(f"  >> {command}")
    sock.sendall((command + "\r").encode("ascii"))
    resp = _read_prompt(sock)
    if verbose:
        print(f"  << {resp!r}")
    ok = resp.rstrip().endswith(">")
    return resp, ok


def send_pgm(sock: socket.socket, command: str) -> tuple[str, bool]:
    """
    Send one line in PROGRAM MODE.
    Returns (raw_response, ok) where ok=True means address number received (no '?').
    """
    print(f"  >> {command}")
    sock.sendall((command + "\r").encode("ascii"))
    resp = _read_pgm(sock)
    ok = "?" not in resp
    print(f"  << {resp!r}  {'OK' if ok else '[?-ERROR]'}")
    return resp, ok


def extract_value(resp: str, sent_cmd: str) -> str:
    """
    Pull the numeric answer from a PR <REG> response.
    The motor echoes the command, then the value, then the prompt.
    """
    for line in resp.splitlines():
        line = line.strip()
        if not line or line == sent_cmd or line in (">", "?"):
            continue
        candidate = line.replace(" ", "")
        if candidate.lstrip("-").isdigit():
            return candidate
    return ""


# ── Error code reference ──────────────────────────────────────────────────────

_ER_MEANINGS = {
    "0":  "No error.",
    "24": "Illegal Data Entered — label name or syntax rejected by firmware.",
    "28": "Trying to redefine an existing program label — CP+S first.",
    "44": "Program locked (LK=1) — CP+S to unlock.",
    "45": "Program space overflow.",
    "46": "Not in Program Mode — LB/MR/H/E sent outside PG block.",
    "64": "Label already exists — CP+S to clear, then re-upload.",
    "73": "Cannot enter/exit PG or run CP while motor is moving — check PR MV.",
    # 79/96 confirmed 2026-09-01 against MCode-V2.06-NOVA.pdf Appendix B,
    # found live on real hardware (main motor +V briefly reading 0-6V):
    "79": "Plus V out of range (too high or too low) — main motor power "
          "supply issue. Check PR VT: format is 'status, AuxV, +VDC'.",
    "92": "Position Error (closed-loop following error exceeded).",
    "96": "Not allowed to start motion with no +V — see ER 79.",
    "86": "Stall detected (open-loop) — drive bridge disabled (DE→0). Only "
          "fires with hMT inactive (AS=0, the factory default) — see ER 104.",
    # 104 confirmed 2026-09-01, same manual, "Locked Rotor" section (p.158)
    # + CF command definition: recover with CF, NOT plain ER=0. Does NOT
    # require a power cycle -- that was an earlier, wrong guess. Only
    # fires when hMT is active (AS=1/2/3) -- see home()'s AS=1 switch.
    "104": "hMT Locked Rotor — commanded to move but the encoder confirms "
           "it isn't (e.g. driven into a hard stop). Clear with CF, not ER=0.",
    "107": "Lag Limit reached (LG exceeded) — closed-loop following error, "
           "hMT active only.",
}

def _er_meaning(er: str) -> str:
    return _ER_MEANINGS.get(er, f"Unknown error code {er} — see MCode-V2.06-NOVA.pdf.")


# ── Startup safety checks ─────────────────────────────────────────────────────

def check_firmware(sock: socket.socket) -> None:
    """PR VR — confirm we're talking to an MDrive device."""
    print("[CHECK] Firmware version (PR VR) ...")
    resp, ok = send_cmd(sock, "PR VR")
    lines = [l.strip() for l in resp.splitlines()
             if l.strip() and l.strip() not in ("PR VR", ">", "?")]
    version = lines[0] if lines else "(not found)"
    if not lines:
        print("[ERROR] No firmware version in PR VR response — device may not be an MDrive.")
        sys.exit(1)
    if not ok:
        print(f"[WARN]  PR VR returned '?' (active error) — firmware: {version}. Will clear error next.")
    else:
        print(f"[OK]    Firmware: {version}\n")


def check_motor_power(sock: socket.socket) -> None:
    """
    PR VT — check that main motor power (+V) is present.
    +V = 0 means the motor power supply is off/disconnected.
    This has happened on this unit before — catch it early rather than
    wondering why motion commands do nothing.
    """
    print("[CHECK] Motor power (PR VT) ...")
    resp, ok = send_cmd(sock, "PR VT")
    if not ok:
        print("[WARN]  PR VT returned '?' — cannot confirm motor power state.")
        return

    # PR VT response format varies by firmware but typically includes the
    # +V reading somewhere in the output.  Print the raw response and let
    # the operator judge; also try to detect an obvious "0" for +V.
    print(f"[INFO]  VT response: {resp!r}")

    # Look for a line that contains "0" that might indicate +V = 0.
    # The exact format is "<status_code> <AuxV> <+V>" or similar.
    lines = [l.strip() for l in resp.splitlines()
             if l.strip() and l.strip() not in ("PR VT", ">", "?")]
    for line in lines:
        parts = line.split()
        if parts and parts[-1] == "0":
            print("[ERROR] +V reads 0 — main motor power supply appears disconnected "
                  "or not energized.")
            print("        Connect/switch on the motor power supply and try again.")
            sock.close()
            sys.exit(1)

    print("[OK]    Motor power appears present.\n")


def check_and_clear_error(sock: socket.socket) -> None:
    """
    PR ER — if non-zero, attempt ER=0 once.
    If it still won't clear, exit: past experience shows some fault states
    require a physical power cycle that the script cannot perform.
    """
    print("[CHECK] Error register (PR ER) ...")
    resp, _ = send_cmd(sock, "PR ER")
    er = extract_value(resp, "PR ER")
    print(f"[INFO]  ER = {er!r}")

    if er in ("0", ""):
        print("[OK]    No active errors.\n")
        return

    print(f"[WARN]  ER = {er} ({_er_meaning(er)}).  Attempting ER=0 ...")
    send_cmd(sock, "ER=0")

    resp2, ok2 = send_cmd(sock, "PR ER")
    er2 = extract_value(resp2, "PR ER")
    if er2 != "0" or not ok2:
        print(f"[ERROR] Error register still {er2!r} after clear attempt.")
        print(f"        {_er_meaning(er2)}")
        print("        This likely requires a physical power cycle.  Exiting.")
        sys.exit(1)

    print("[OK]    Error cleared.\n")


def ensure_drive_enabled(sock: socket.socket) -> None:
    """
    PR DE — if drive is disabled (0), attempt DE=1 once.
    If still disabled, exit.
    """
    print("[CHECK] Drive enable (PR DE) ...")
    resp, _ = send_cmd(sock, "PR DE")
    de = extract_value(resp, "PR DE")
    print(f"[INFO]  DE = {de!r}")

    if de == "1":
        print("[OK]    Drive is enabled.\n")
        return

    print("[WARN]  Drive disabled.  Attempting DE=1 ...")
    send_cmd(sock, "DE=1")

    resp2, _ = send_cmd(sock, "PR DE")
    de2 = extract_value(resp2, "PR DE")
    if de2 != "1":
        print("[ERROR] Drive still disabled after DE=1 — possible stall or hardware fault.")
        print("        A power cycle may be required.  Exiting.")
        sys.exit(1)

    print("[OK]    Drive enabled.\n")


def startup_checks(sock: socket.socket) -> None:
    """Run all safety checks before allowing any motion command."""
    check_firmware(sock)
    check_motor_power(sock)
    check_and_clear_error(sock)
    ensure_drive_enabled(sock)


# ── Motion helpers ────────────────────────────────────────────────────────────

def wait_for_stop(sock: socket.socket, label: str = "move") -> None:
    """
    Poll PR MV until the motor reports stopped (MV=0), or MOVE_TIMEOUT_S elapses.
    Sends SL 0 and raises RuntimeError on timeout.
    """
    print(f"[POLL]  Waiting for '{label}' to complete ...")
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp, _ = send_cmd(sock, "PR MV", verbose=False)
        if extract_value(resp, "PR MV") == "0":
            print("[OK]    Move complete.\n")
            return
        time.sleep(POLL_INTERVAL_S)

    print(f"[ERROR] Move '{label}' timed out after {MOVE_TIMEOUT_S:.0f}s.  Sending SL 0.")
    send_cmd(sock, "SL 0", verbose=False)
    raise RuntimeError(f"Move timeout: {label}")


def print_position(sock: socket.socket) -> int:
    """Read and display the current position counter (PR P)."""
    resp, _ = send_cmd(sock, "PR P", verbose=False)
    val = extract_value(resp, "PR P")
    pos = int(val) if val.lstrip("-").isdigit() else 0
    print(f"[POS]   {pos:+d} steps from zero")
    return pos


def print_velocity(sock: socket.socket) -> None:
    """
    Read and display the motion profile actually governing G1/G2 right now:
    VM (max velocity), VI (initial velocity), A (acceleration), D (deceleration)
    — all in steps/sec (or steps/sec^2 for A/D). None of these are set anywhere
    in this file's upload_programs(), so G1/G2 run at whatever the motor's
    factory/flash default is. This queries the real, current values instead
    of guessing.
    """
    for reg, label in (("VM", "Max velocity (VM)"), ("VI", "Initial velocity (VI)"),
                        ("A", "Acceleration (A)"), ("D", "Deceleration (D)")):
        resp, _ = send_cmd(sock, f"PR {reg}", verbose=False)
        val = extract_value(resp, f"PR {reg}")
        print(f"[VEL]   {label:24s} = {val or '(no response)'}")


def run_program(sock: socket.socket, label: str, description: str) -> bool:
    """
    Execute a stored onboard program by label name and wait for completion.
    E.g. run_program(sock, "G1", "drop (forward)")

    Returns True if EX was accepted and the move completed, False if EX
    itself was rejected (e.g. ER=30 Unknown Label — the program was never
    actually flashed/labeled). Callers that need to know whether real
    motion happened (not just print a status line) must check this —
    2026-09-01 finding: home_onboard() used to trust PR P's value
    unconditionally after calling this, which produced a misleading
    "[OK] found the hard stop" even though EX G3 had just been rejected —
    the position it read was simply left over from an earlier, real run.
    """
    print(f"[RUN]   EX {label}  ({description}) ...")
    _, ok = send_cmd(sock, f"EX {label}")
    if not ok:
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] EX {label} rejected.  ER = {er} — {_er_meaning(er)}")
        print("        Have you run 'upload' yet?  Programs must be flashed before use.")
        return False
    wait_for_stop(sock, description)
    print_position(sock)
    return True


def run_manual_move(sock: socket.socket, steps: int) -> None:
    """
    Immediate-mode MR <steps> — a raw RELATIVE move for an arbitrary step
    count, bypassing G1/G2 entirely. No re-flash needed to try a distance
    from wherever the carriage currently is -- useful for dialing in a new
    ASPIRE_STEPS value before committing to it (same idea as this file's
    own step-count tuning history, see the ASPIRE_STEPS comment above).
    Deliberately still relative even though G1/G2 themselves are absolute
    (2026-09-07) -- this command's whole job is testing an arbitrary
    distance from the current position, which only makes sense relative.

    Sign convention matches ASPIRE_STEPS: positive = aspire / reverse,
    negative = drop / forward.

    Explicitly sets VM/A/D to VELOCITY_MAX/ACCEL/DECEL first (the same
    values G1/G2's own onboard bodies use) rather than trusting whatever
    profile happens to be ambient on the drive -- home()/home2 always
    restore this after themselves, but being explicit here means a
    manual move's timing is always genuinely comparable to production,
    regardless of what ran before it.
    """
    print(f"[MOVE]  Setting VM={VELOCITY_MAX} A={ACCEL} D={DECEL} (production profile) ...")
    send_cmd(sock, f"VM={VELOCITY_MAX}", verbose=False)
    send_cmd(sock, f"A={ACCEL}", verbose=False)
    send_cmd(sock, f"D={DECEL}", verbose=False)

    print(f"[MOVE]  MR {steps:+d} ...")
    _, ok = send_cmd(sock, f"MR {steps}")
    if not ok:
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] MR {steps} rejected.  ER = {er} — {_er_meaning(er)}")
        return
    wait_for_stop(sock, f"manual move ({steps:+d} steps)")
    print_position(sock)


# ── Upload ────────────────────────────────────────────────────────────────────

def _pgm_or_die(sock: socket.socket, command: str) -> None:
    """
    Send one program-mode line.  If the motor responds with '?' (error):
      1. Read PR ER for diagnosis (must exit program mode first to use immediate mode).
      2. Print which line failed and the ER value.
      3. Exit.
    """
    _, ok = send_pgm(sock, command)
    if not ok:
        print(f"[ERROR] Program line '{command}' was rejected.")
        # Exit program mode cleanly so PR ER works (immediate mode command).
        print("        Exiting program mode (PG) to read error register ...")
        try:
            sock.sendall(b"PG\r")
            time.sleep(0.5)
            sock.recv(256)   # drain PG exit response
        except Exception:
            pass
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"        ER = {er} — {_er_meaning(er)}")
        sys.exit(1)


def upload_programs(sock: socket.socket) -> None:
    """
    Flash four programs to the motor's non-volatile flash memory:
      G1  →  VM=/A=/D= + MA 0              (drop / forward / dispense -- absolute move TO HOME)
      G2  →  VM=/A=/D= + MA ASPIRE_STEPS   (aspire / reverse / draw -- absolute move FROM home)
      G3  →  home-to-hard-stop (2026-09-01, see below)
      G4  →  G3's internal success handler (not meant to be EX'd directly)

    G1/G2 switched from relative (MR) to absolute (MA) 2026-09-07, once G3
    made P=0 a reliable, physically-verified reference -- see the
    ASPIRE_STEPS comment near the top of this file for the full reasoning.

    Full sequence:
      1.  Check motor is not moving (Error 73 if CP/PG run while moving)
      2.  CP + S    — wipe any existing/corrupt programs
      3.  PG 1      — enter program mode
      4.  Write programs line by line (each checked for '?' error)
      5.  PG        — exit program mode
      6.  S         — commit to flash (survives power cycles)

    Label choice: G1/G2/G3/G4 — 'G' prefix confirmed safe in LMDAE firmware.
    Avoid: F* (reserved, causes ER=24), R* (RT = return cmd), SU (auto-runs on boot).
    CP wipes ALL existing programs at once, so every upload re-flashes G1+G2
    even when only G3/G4 actually changed — this is why they're all written
    together in one pass rather than G3/G4 being addable independently.

    G1/G2: sets VM/A/D (velocity profile), then MA (the absolute move),
    then H (hold — waits for move to finish), then E (end). H is CRITICAL:
    without it the program exits before the carriage stops moving.

    G3 (home): the ONBOARD equivalent of this same file's home() function
    (see its docstring for the full story — hMT is off by default on this
    drive, AS=1 turns on closed-loop Locked Rotor detection, LT shortens
    the reaction time from 2000ms to 50ms). Confirmed working via home()
    on real hardware 2026-09-01 before being ported here — this MCode body
    is the same sequence, just running autonomously on the motor instead
    of being orchestrated command-by-command from the PC:
      1. Er=0 — clear any stale error
      2. AS=1, LT=50, RC=15, VM/A/D lowered — enter the safe seek profile
      3. MR -2000000 — seek toward the dispense-end hard stop
      4. H — hold until the move stops (full distance OR a Locked Rotor cutoff)
      5. BR G4, Er=104 — if Locked Rotor fired (the expected/good outcome),
         jump to G4 to finish; otherwise fall through
      6. Fallthrough path (no fault — seek ran the full distance without
         ever contacting anything): restore the normal profile and end
         WITHOUT touching P — never trust an un-stalled seek as home.
      7. G4 (the branch target): CF (clear the Locked Rotor fault, same as
         the manual's own recovery instruction — NOT plain Er=0), then
         MR HOME_BACKOFF_STEPS + H (back off 3mm toward aspiration,
         David's suggestion 2026-09-01 — see the HOME_BACKOFF_STEPS
         comment above home() for why: without this, every NORMAL drop
         cycle, not just homing, was landing right at the true hard stop,
         since G1 (drop) always targets P=0 directly), THEN P=0 (this
         backed-off position is home, not the touch point), then restore
         the normal profile and end.
    RC=25 / VM=2400000 / A=4000000 / D=4000000 / AS=0 / LT=2000 restored at
    the end of BOTH paths — these are this motor's confirmed-current
    production baseline (VM/A/D match VELOCITY_MAX/ACCEL/DECEL below; RC/
    AS/LT match what was read back off the drive before any of this
    session's testing touched them).
    """

    # ── Guard: must not be moving ─────────────────────────────────────────────
    print("[UPLOAD] Checking motor is stopped (PR MV) ...")
    resp_mv, _ = send_cmd(sock, "PR MV")
    if extract_value(resp_mv, "PR MV") != "0":
        print("[ERROR] Motor is currently moving (MV=1).  Wait for it to stop before uploading.")
        sys.exit(1)
    print("[OK]    Motor is stopped.\n")

    # ── Clear existing programs ───────────────────────────────────────────────
    # Previous partial uploads may have left corrupt data.
    # CP clears program RAM; S saves the cleared state to flash.
    print("[UPLOAD] Clearing program space (CP + S) ...")
    _, ok_cp = send_cmd(sock, "CP")
    if not ok_cp:
        print("[ERROR] CP rejected — motor may be moving or program space is locked.")
        sys.exit(1)
    _, ok_s = send_cmd(sock, "S")
    if not ok_s:
        print("[ERROR] S (save) rejected after CP.")
        sys.exit(1)
    print("[OK]    Program space cleared.\n")

    # ── Enter program mode ────────────────────────────────────────────────────
    # 'PG 1' switches the motor into program-recording mode starting at address 1.
    # From this point the motor responds with ADDRESS NUMBERS, not '>' prompts.
    # All writes go to RAM until we exit with 'PG' and commit with 'S'.
    print("[UPLOAD] Entering program mode (PG 1) ...")
    resp_pg, ok_pg = send_pgm(sock, "PG 1")
    if not ok_pg:
        # PG itself can fail — read ER in immediate mode (PG not entered yet).
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] Could not enter program mode.  ER = {er} — {_er_meaning(er)}")
        sys.exit(1)
    print("[OK]    In program mode.\n")

    # ── Write G1/G2 (simple 3-line programs) ──────────────────────────────────
    # VM/A/D set INSIDE each program body — separate onboard programs, so
    # each must set its own velocity profile explicitly rather than relying
    # on whatever an earlier immediate-mode command left behind.
    # MA not MR (2026-09-07, see the ASPIRE_STEPS comment above) — G1 always
    # returns to the single true home position; G2 always goes to the same
    # fixed absolute position measured from home. Neither depends on
    # wherever the carriage happened to be beforehand.
    simple_programs = [
        # (label,  move_cmd,                  description)
        ("G1", "MA 0",                f"drop / forward  (absolute -> home, P=0)"),
        ("G2", f"MA {ASPIRE_STEPS}",  f"aspire / reverse (absolute -> {ASPIRE_STEPS})"),
    ]

    for label, move_cmd, desc in simple_programs:
        print(f"  -- Program '{label}'  ({desc})")
        _pgm_or_die(sock, f"LB {label}")   # declare label
        _pgm_or_die(sock, f"VM={VELOCITY_MAX}")   # max velocity
        _pgm_or_die(sock, f"A={ACCEL}")           # acceleration
        _pgm_or_die(sock, f"D={DECEL}")           # deceleration
        _pgm_or_die(sock, move_cmd)         # motion instruction
        _pgm_or_die(sock, "H")              # hold: wait for move to finish
        _pgm_or_die(sock, "E")              # end program
        print()

    # ── Write G3/G4 (home-to-hard-stop, see docstring above) ──────────────────
    print("  -- Program 'G3' (home to dispense-end hard stop, hMT Locked Rotor)")
    restore_lines = [
        f"VM={VELOCITY_MAX}", f"A={ACCEL}", f"D={DECEL}",
        "AS=0", "LT=2000", "RC=25",
    ]
    g3_lines = [
        "LB G3",   # declare the label -- EX G3 fails with ER=30 (Unknown
                   # Label) without this; missing on the first upload attempt.
        "Er=0",
        "AS=1", "LT=50", "RC=15",
        f"VM={HOME_SEEK_VELOCITY}", f"A={HOME_SEEK_ACCEL}", f"D={HOME_SEEK_ACCEL}",
        f"MR {-HOME_SEEK_STEPS}",
        "H",
        "BR G4, Er=104",
        *restore_lines,   # no fault -- do NOT touch P, just restore and end
        "E",
    ]
    for line in g3_lines:
        _pgm_or_die(sock, line)
    print()

    print("  -- Program 'G4' (G3's success handler -- not meant to be EX'd directly)")
    g4_lines = [
        "CF",                                # clear the Locked Rotor fault, re-enable bridges
        f"MR {HOME_BACKOFF_STEPS}",          # back off ~2mm toward aspiration -- see
                                              # HOME_BACKOFF_STEPS comment above home()
        "H",                                 # wait for the backoff move to finish
        "P=0",                               # THIS backed-off position is home, not the touch point
        *restore_lines,
        "E",
    ]
    for label_or_line in ["LB G4", *g4_lines]:
        _pgm_or_die(sock, label_or_line)
    print()

    # ── Exit program mode ─────────────────────────────────────────────────────
    # 'PG' (no address) switches back to immediate mode — '>' prompt returns.
    print("[UPLOAD] Exiting program mode (PG) ...")
    _, ok_exit = send_cmd(sock, "PG")
    if not ok_exit:
        print("[ERROR] Could not exit program mode — motor may be in an unknown state.")
        sys.exit(1)
    print("[OK]    Back in immediate mode.\n")

    # ── Save to flash ─────────────────────────────────────────────────────────
    # Without 'S' the programs exist only in RAM and are lost on power cycle.
    print("[UPLOAD] Saving to non-volatile flash (S) ...")
    _, ok_save = send_cmd(sock, "S")
    if not ok_save:
        print("[ERROR] Save (S) rejected — programs NOT persisted to flash.")
        sys.exit(1)

    print("\n[UPLOAD] Done.")
    print(f"         G1 = drop   (MA 0, absolute -> home)")
    print(f"         G2 = aspire (MA {ASPIRE_STEPS}, absolute from home)")
    print(f"         G3 = home   (seek dispense-end hard stop, hMT Locked Rotor)")
    print(f"         VM={VELOCITY_MAX}  A={ACCEL}  D={DECEL}  (G1/G2, and G3's restored baseline)")
    print("         Programs survive power cycles.  Re-run 'upload' if step counts OR velocity change.\n")


# ── Homing (2026-08-28) ────────────────────────────────────────────────────────
# No limit switch exists on this actuator -- G1/G2 are pure relative moves
# (MR), so the drive's own position counter (P) only ever reflects what it
# THINKS it did, not where the carriage actually is. Any stall, timeout, or
# interrupted move (e.g. an operator disconnect mid-cycle) silently shifts
# the effective zero from then on -- reported live 2026-08-28 as "the
# syringe stays pulled back a little and that's become normal."
#
# The only way to establish a true, drift-proof zero without a limit switch
# is to deliberately drive the carriage into its own mechanical hard stop
# and let the drive detect that it got there, then redefine that physical
# position as P=0.
# Operator-confirmed (2026-08-28): the DISPENSE end (G1 / forward
# direction) is the safe end to home against -- do NOT home against the
# aspirate/backward end without separately confirming that end is safe too.
#
# Runs at reduced velocity/accel/current for this ONE seek move only, then
# restores whatever profile was already configured -- hitting a hard stop
# at full production speed/torque risks mechanical wear on every homing run.
#
# 2026-09-01 real-hardware finding, first live test of this function: at
# RC=30 the carriage visibly reached the syringe and kept grinding against
# it for several seconds, then simply stopped once the seek move ran out of
# its own commanded distance -- PR ER read 0 the whole time, no fault ever
# fired. Root cause, confirmed against the actual manual (Software/
# MCode-V2.06-NOVA.pdf, one level up from this repo, Appendix B + the hMT
# chapter): this drive is hMT-*capable* (it has an encoder) but hMT
# closed-loop monitoring is OFF by default -- AS (hMT Mode) defaults to 0,
# "hMT inactive: motor performs as a traditional stepper." With hMT off,
# NOTHING watches the encoder for "commanded to move but isn't" -- not the
# open-loop stall detector (ER=86, what this function used to check for)
# and not the closed-loop Locked Rotor detector either. The drive just
# blindly executes the commanded step profile regardless of what the
# carriage is actually doing.
#
# Fix: temporarily switch to AS=1 ("Fixed Current" hMT mode -- uses RC
# directly, and critically does NOT auto-start motion the instant it's set,
# unlike AS=3 "Torque mode", which the manual flags with an explicit hazard
# warning: "Motion will commence IMMEDIATELY upon setting AS=3"). With hMT
# active, a genuine jam now raises ER=104 "Locked Rotor" -- the manual's own
# words: "no rotor movement while at the maximum allowed lag for a
# specified period of time" -- exactly "commanded to move, encoder says it
# isn't." Recovered with CF (Clear Locked Rotor Fault), NOT the plain
# ER=0/DE=1 pattern -- confirmed via the manual's CF command definition.
# Also shortens LT (Locked Rotor Timeout, how long the drive tolerates
# "at max lag, no movement" before cutting power) from its 2000ms (2 full
# second) default down to 50ms, the manual's own example value -- 2 seconds
# of tolerated grinding before the old default would even react was likely
# a real part of what made the first test as bad as it was. AS/LT are
# saved and restored afterward exactly like VM/A/D/RC below -- G1/G2's
# normal production moves keep running in the plain AS=0 mode they've
# always used, completely untouched by this.
#
# Run current also lowered 30 -> 15 (operator's own real-hardware call,
# 2026-09-01 -- "we can decrease the power even more"): even with hMT
# detection now active, current still needs to be low enough that the
# rotor genuinely CAN'T keep turning against the hard stop (a true stall/
# lock), rather than merely slipping/grinding through it at reduced but
# still-sufficient torque. Tune further from here based on what you hear.
#
# HOME_SEEK_STEPS is deliberately larger than any step count ever recorded
# for this actuator (max seen: 1,575,000, per architecture/motor.md) so the
# seek move cannot possibly complete without hitting something -- but the
# true mechanical travel limit is still unconfirmed. Watch closely on every
# real run; Ctrl+C sends SL 0 at any time (see main()'s except block).
#
# HOME_BACKOFF_STEPS (2026-09-01, David's suggestion, relayed by operator):
# P=0 was originally set at the EXACT point of contact with the hard stop.
# Problem: G1/G2 are relative moves (MR) tuned as a matched forward/back
# pair, so DROP always lands back at roughly this same reference point --
# meaning every single NORMAL drop cycle, not just the once-per-startup
# home, was landing the carriage right at (or essentially at) the true
# hard stop. Fix, standard practice for hard-stop homing: after confirming
# the stop (Locked Rotor), back OFF a small distance before declaring
# P=0 -- so the zero every future relative move is built on sits a small
# clearance short of the real mechanical limit, and only this one-time
# homing sequence ever actually touches it. 40,000 steps = 2mm (operator-
# measured conversion, 2026-09-01) -- towards ASPIRATION (positive
# direction, same sign as ASPIRE_STEPS), i.e. away from the dispense-end
# hard stop this routine homes against.
HOME_SEEK_STEPS      = 2_000_000   # forward -- past any step count ever used
HOME_SEEK_VELOCITY   = 300_000     # steps/sec -- conservative, well under production VM
HOME_SEEK_ACCEL      = 500_000     # steps/sec^2
HOME_RUN_CURRENT_PCT = 15          # % -- lowered from 30, see comment above
HOME_LOCKED_ROTOR_TIMEOUT_MS = 50  # LT -- default is 2000ms, see comment above
HOME_BACKOFF_STEPS   = 40_000      # = 2mm, toward aspiration -- see comment above


def home(sock: socket.socket) -> None:
    """
    Home the carriage against its dispense-end (forward) hard stop, back
    off HOME_BACKOFF_STEPS (~2mm) toward aspiration, and redefine THAT
    position as P=0 -- not the touch point itself. See the HOME_* comment
    block above for why forward specifically, the AS/LT hMT switch, the
    current/velocity caveats, and why the backoff exists (David's
    suggestion: G1/G2 are a matched relative-move pair, so without this,
    every normal drop cycle -- not just homing -- was landing right at the
    real hard stop).
    """
    print("[HOME]  Homing against the DISPENSE (forward) hard stop.")
    print("        Watch closely — Ctrl+C sends SL 0 (stop) at any time.\n")

    # Save the current velocity/mode profile so it can be restored
    # afterward — same save/restore pattern as upload_programs(), just
    # read instead of hardcoded. AS/LT added 2026-09-01, see comment above.
    saved: dict[str, str] = {}
    for reg in ("VM", "A", "D", "AS", "LT"):
        resp, _ = send_cmd(sock, f"PR {reg}", verbose=False)
        saved[reg] = extract_value(resp, f"PR {reg}")

    resp_rc, ok_rc_read = send_cmd(sock, "PR RC", verbose=False)
    rc_val = extract_value(resp_rc, "PR RC")
    rc_supported = ok_rc_read and rc_val != ""
    if rc_supported:
        saved["RC"] = rc_val
        send_cmd(sock, f"RC={HOME_RUN_CURRENT_PCT}")
    else:
        print("[WARN]  RC (run current) register not readable on this drive/firmware —"
              " homing at whatever current is already configured. If you are not"
              " confident that's safe for a hard-stop impact, Ctrl+C now.\n")

    print(f"[HOME]  Enabling hMT closed-loop detection (AS=1, fixed current) and "
          f"setting Locked Rotor Timeout to {HOME_LOCKED_ROTOR_TIMEOUT_MS}ms "
          f"(default 2000ms) ...")
    send_cmd(sock, "AS=1")
    send_cmd(sock, f"LT={HOME_LOCKED_ROTOR_TIMEOUT_MS}")

    send_cmd(sock, f"VM={HOME_SEEK_VELOCITY}")
    send_cmd(sock, f"A={HOME_SEEK_ACCEL}")
    send_cmd(sock, f"D={HOME_SEEK_ACCEL}")

    print(f"[HOME]  Seeking hard stop: MR {-HOME_SEEK_STEPS} ...")
    send_cmd(sock, f"MR {-HOME_SEEK_STEPS}")

    # Poll for MV=0 (stopped) — either a real Locked Rotor fault (expected,
    # see ER check below), or (unexpectedly) having actually completed the
    # full commanded distance without ever hitting anything.
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    stopped = False
    while time.monotonic() < deadline:
        resp, _ = send_cmd(sock, "PR MV", verbose=False)
        if extract_value(resp, "PR MV") == "0":
            stopped = True
            break
        time.sleep(POLL_INTERVAL_S)

    if not stopped:
        print(f"[ERROR] Home-seek move did not stop within {MOVE_TIMEOUT_S:.0f}s. Sending SL 0.")
        send_cmd(sock, "SL 0", verbose=False)
        _restore_profile(sock, saved)
        raise RuntimeError("Home-seek timeout")

    resp_er, _ = send_cmd(sock, "PR ER")
    er = extract_value(resp_er, "PR ER")

    if er == "104":
        print("[OK]    Locked Rotor detected (ER=104) — hard stop reached, encoder "
              "confirmed the carriage genuinely stopped moving. Clearing with CF ...")
        send_cmd(sock, "CF")
        print(f"[HOME]  Backing off {HOME_BACKOFF_STEPS} steps (~2mm) toward aspiration, "
              f"away from the true hard stop (David's suggestion, 2026-09-01) ...")
        send_cmd(sock, f"MR {HOME_BACKOFF_STEPS}")
        wait_for_stop(sock, "home backoff")
        send_cmd(sock, "P=0")
        print(f"[OK]    Position redefined as 0, {HOME_BACKOFF_STEPS} steps (~2mm) clear "
              f"of the true dispense hard stop.")
    elif er == "86":
        # Shouldn't normally fire now that AS=1 is set (86 is the
        # open-loop detector, which mainly matters when hMT is off) — kept
        # as a fallback in case it fires anyway for some other reason.
        # Backs off the same as the ER=104 path above -- same reasoning.
        print("[OK]    Stall detected (ER=86) — hard stop reached as expected.")
        send_cmd(sock, "ER=0")
        send_cmd(sock, "DE=1")
        print(f"[HOME]  Backing off {HOME_BACKOFF_STEPS} steps (~2mm) toward aspiration, "
              f"away from the true hard stop ...")
        send_cmd(sock, f"MR {HOME_BACKOFF_STEPS}")
        wait_for_stop(sock, "home backoff")
        send_cmd(sock, "P=0")
        print(f"[OK]    Position redefined as 0, {HOME_BACKOFF_STEPS} steps (~2mm) clear "
              f"of the true dispense hard stop.")
    elif er in ("0", ""):
        print(f"[WARN]  Motor stopped WITHOUT any fault (ER=0) — it completed the full "
              f"{HOME_SEEK_STEPS} step seek move without the drive ever detecting a jam. "
              f"P=0 was NOT set — do not trust this as home. If you felt/heard it hit "
              f"something, HOME_RUN_CURRENT_PCT may still be high enough to grind "
              f"through even with hMT active — try lowering it further. If it truly "
              f"never reached anything, HOME_SEEK_STEPS may need to be larger.")
    else:
        print(f"[ERROR] Unexpected error during home-seek: ER={er} ({_er_meaning(er)}).")
        if er == "104":
            send_cmd(sock, "CF")
        else:
            send_cmd(sock, "ER=0")

    _restore_profile(sock, saved)
    print_position(sock)


def _restore_profile(sock: socket.socket, saved: dict) -> None:
    print("[HOME]  Restoring previous velocity/current profile ...")
    for reg, val in saved.items():
        if val:
            send_cmd(sock, f"{reg}={val}")


def home_onboard(sock: socket.socket) -> None:
    """
    Trigger the ONBOARD G3 home routine (see upload_programs()'s docstring
    for the full MCode body) via a single EX G3 — exactly the same trigger
    sort_server.prg's MotorHome uses once this is wired into production.
    Requires 'upload' to have been run at least once since G3/G4 were added
    (they're written together with G1/G2 since CP wipes everything).

    G3 handles its own success/fallback logic on-device (BR on Er=104, CF +
    P=0 on success, restore-without-P=0 on fallback) — by the time EX G3
    returns, PR ER has already been cleared either way, so the only way to
    tell which path it took from here is the position: P=0 on success,
    still nonzero if it fell through without ever finding the hard stop.
    Run this to confirm the FLASHED version behaves the same as the
    already-confirmed-working home() command before trusting it in
    production.
    """
    print("[HOME]  Triggering onboard G3 (home) ...")
    ran = run_program(sock, "G3", "home (onboard)")
    if not ran:
        # EX G3 itself was rejected (e.g. ER=30 Unknown Label — not
        # actually flashed) — nothing moved, so PR P below reflects
        # whatever was left over from a previous run, not this attempt.
        # Do NOT interpret it as a result here — see run_program()'s
        # docstring for the 2026-09-01 false-positive this fixes.
        print("[ERROR] G3 never actually ran — see the rejection above. "
              "Re-run 'upload' and try again.")
        return
    pos = print_position(sock)
    if pos == 0:
        print("[OK]    Position is 0 — G3 found the hard stop (Locked Rotor), backed off "
              f"{HOME_BACKOFF_STEPS} steps (~2mm), and set THAT as home, same as the "
              "pure-Python home() command.")
    else:
        print(f"[WARN]  Position is {pos:+d}, not 0 — G3 likely fell through "
              f"WITHOUT ever detecting the hard stop (see upload_programs()'s "
              f"docstring for that fallback path). Do not trust this as home.")


# ── Interactive loop ──────────────────────────────────────────────────────────

_HELP = """
  Commands:
    drop    → execute G1  (forward  -800000 steps — dispense/drop)
    aspire  → execute G2  (reverse  +800000 steps — draw/aspire)
    pos     → print current position (no motion)
    speed   → print VM/VI/A/D — the actual current aspirate/drop velocity
              profile (no motion). Never set in this file, so this is
              whatever the motor's factory/flash default is.
    zero    → set current position as the new 0  (P=0, no motion)
    home    → drive to the dispense-end hard stop and set THAT as 0,
              orchestrated command-by-command from this script (real
              motion toward the forward limit — see HOME_* comment above
              main()'s home() function before running this)
    home2   → same as 'home', but triggers the ONBOARD G3 program (EX G3)
              instead — run 'upload' first. Use this to confirm the
              flashed version matches before wiring it into sort_server.prg.
    move <n> → raw MR <n> (RELATIVE, from wherever the carriage currently
              is) at the production velocity profile, bypassing G1/G2
              entirely -- no re-flash needed to try a step count other
              than ASPIRE_STEPS. Positive = aspire (reverse), negative =
              drop (forward), same sign convention as that constant.
              E.g. 'move 50000' or 'move -50000'.
    q       → quit
"""

def interactive_loop(sock: socket.socket) -> None:
    print("[RUN]   Interactive mode.")
    print(_HELP)
    while True:
        try:
            cmd = input("linear> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[QUIT]")
            break

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "drop":
            run_program(sock, "G1", "drop (forward)")
        elif cmd == "aspire":
            run_program(sock, "G2", "aspire (reverse)")
        elif cmd == "pos":
            print_position(sock)
        elif cmd == "speed":
            print_velocity(sock)
        elif cmd == "zero":
            _, ok = send_cmd(sock, "P=0")
            if ok:
                print("[OK]    Current position redefined as 0.")
            else:
                print("[ERROR] Could not set P=0.")
        elif cmd == "home":
            try:
                home(sock)
            except RuntimeError as exc:
                print(f"[FATAL] {exc}")
        elif cmd == "home2":
            home_onboard(sock)
        elif cmd.startswith("move"):
            arg = cmd[len("move"):].strip()
            try:
                steps = int(arg)
            except ValueError:
                print("  Usage: move <steps>  (e.g. 'move 50000' or 'move -50000')")
            else:
                run_manual_move(sock, steps)
        elif cmd == "":
            pass
        else:
            print(f"  Unknown: '{cmd}'")
            print(_HELP)


# ── Entry point ───────────────────────────────────────────────────────────────

_USAGE = """
Usage:
  python hardware_tests/linear_control.py upload    # flash G1+G2+G3+G4 to motor (run once)
  python hardware_tests/linear_control.py drop      # forward  -800000 steps
  python hardware_tests/linear_control.py aspire    # reverse  +800000 steps
  python hardware_tests/linear_control.py move <n>  # raw MR <n>, any step count, no re-flash
                                                      # needed -- positive = aspire, negative =
                                                      # drop. E.g. "move 50000" or "move -50000"
  python hardware_tests/linear_control.py pos       # print position, no motion
  python hardware_tests/linear_control.py speed     # print VM/VI/A/D velocity profile, no motion
  python hardware_tests/linear_control.py home      # drive to dispense hard stop, set P=0 there
                                                      # (orchestrated from this script)
  python hardware_tests/linear_control.py home2     # same, but via the ONBOARD G3 program
                                                      # (EX G3) -- run 'upload' first
  python hardware_tests/linear_control.py run       # interactive control loop
"""

def main() -> None:
    if len(sys.argv) < 2:
        print(_USAGE)
        sys.exit(0)

    sub = sys.argv[1].lower()
    if sub not in {"upload", "drop", "aspire", "move", "pos", "speed", "home", "home2", "run"}:
        print(f"Unknown sub-command: '{sub}'\n{_USAGE}")
        sys.exit(1)

    move_steps: int | None = None
    if sub == "move":
        if len(sys.argv) < 3:
            print(f"Missing step count.\n{_USAGE}")
            sys.exit(1)
        try:
            move_steps = int(sys.argv[2])
        except ValueError:
            print(f"'{sys.argv[2]}' is not a valid integer step count.\n{_USAGE}")
            sys.exit(1)

    sock = connect()

    try:
        if sub == "upload":
            # For upload: run full startup checks, then flash programs.
            startup_checks(sock)
            upload_programs(sock)
        else:
            # For all motion/query sub-commands: safety checks first.
            startup_checks(sock)

            if sub == "drop":
                run_program(sock, "G1", "drop (forward)")
            elif sub == "aspire":
                run_program(sock, "G2", "aspire (reverse)")
            elif sub == "move":
                assert move_steps is not None
                run_manual_move(sock, move_steps)
            elif sub == "pos":
                print_position(sock)
            elif sub == "speed":
                print_velocity(sock)
            elif sub == "home":
                home(sock)
            elif sub == "home2":
                home_onboard(sock)
            elif sub == "run":
                interactive_loop(sock)

    except KeyboardInterrupt:
        print("\n[ABORT] Ctrl+C — attempting safe stop (SL 0) ...")
        try:
            send_cmd(sock, "SL 0", verbose=False)
        except Exception:
            pass

    except RuntimeError as exc:
        print(f"[FATAL] {exc}")
        sys.exit(1)

    finally:
        print("[CONNECT] Socket closed.")
        sock.close()


if __name__ == "__main__":
    main()
