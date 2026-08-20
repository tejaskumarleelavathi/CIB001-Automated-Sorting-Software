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
  G1  →  MR -1575000  (drop / dispense — carriage moves forward)
  G2  →  MR +1575000  (aspire / draw — carriage moves backward)

Python just sends "EX G1" or "EX G2" to trigger them.  The motor
executes the move autonomously and signals completion via the ">" prompt.

Labels G1 / G2 chosen because:
  - G prefix is not a reserved MCode mnemonic in LMDAM/LMDAE firmware
  - Lessons from first motor: F* prefix triggers ER=24 (reserved); A* safe,
    G* also safe — use G here to distinguish from the rotary motor's A* labels

Usage:
  python hardware_tests/linear_control.py upload    # flash G1+G2 to motor (run once)
  python hardware_tests/linear_control.py drop      # execute G1 (forward 1575000 steps)
  python hardware_tests/linear_control.py aspire    # execute G2 (reverse 1575000 steps)
  python hardware_tests/linear_control.py pos       # print position, no motion
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
# the same amount every time to stay symmetric. This is now ~7.9x the last
# value actually confirmed safe by hand-jogging (200,000) and lead screw
# pitch/travel limit is still unconfirmed — watch closely for a stall or
# hard-stop collision on the first real run at this value.
DROP_STEPS   = -1_575_000   # forward / dispense
ASPIRE_STEPS =  1_575_000   # reverse / draw

# Velocity profile (steps/sec, steps/sec^2) — governs how FAST G1/G2 run,
# completely separate from DROP_STEPS/ASPIRE_STEPS above (which only govern
# how FAR). Never set anywhere in this file before 2026-08-19 — motor was
# running at factory/flash default the whole time (VM=768000, VI=1000,
# A=D=1000000). Increased to VM=1,200,000/A=D=2,000,000 same day, then VM
# 2x'd -> 2,400,000 (A/D left unchanged — operator asked to increase
# "speed" specifically, not acceleration) same day, testing configs for the
# tip-disturbance issue (DEV-16). Baked into each program body (not set
# once) since G1/G2 are separate onboard programs and each must set its own
# profile explicitly.
VELOCITY_MAX =  2_400_000   # VM
ACCEL        =  2_000_000   # A
DECEL        =  2_000_000   # D

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
    "86": "Stall detected — drive bridge disabled (DE→0).",
    "104": "hMT Locked Rotor — power cycle required.",
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


def run_program(sock: socket.socket, label: str, description: str) -> None:
    """
    Execute a stored onboard program by label name and wait for completion.
    E.g. run_program(sock, "G1", "drop (forward)")
    """
    print(f"[RUN]   EX {label}  ({description}) ...")
    _, ok = send_cmd(sock, f"EX {label}")
    if not ok:
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] EX {label} rejected.  ER = {er} — {_er_meaning(er)}")
        print("        Have you run 'upload' yet?  Programs must be flashed before use.")
        return
    wait_for_stop(sock, description)
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
    Flash two programs to the motor's non-volatile flash memory:
      G1  →  VM=/A=/D= + MR -1575000   (drop / forward / dispense)
      G2  →  VM=/A=/D= + MR +1575000   (aspire / reverse / draw)

    Full sequence:
      1.  Check motor is not moving (Error 73 if CP/PG run while moving)
      2.  CP + S    — wipe any existing/corrupt programs
      3.  PG 1      — enter program mode
      4.  Write programs line by line (each checked for '?' error)
      5.  PG        — exit program mode
      6.  S         — commit to flash (survives power cycles)

    Label choice: G1/G2 — 'G' prefix confirmed safe in LMDAE firmware.
    Avoid: F* (reserved, causes ER=24), R* (RT = return cmd), SU (auto-runs on boot).
    Each program sets VM/A/D (velocity profile), then MR (the move), then
    H (hold — waits for move to finish), then E (end). H is CRITICAL:
    without it the program exits before the carriage stops moving.
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

    # ── Write programs ────────────────────────────────────────────────────────
    # VM/A/D set INSIDE each program body — G1 and G2 are separate onboard
    # programs, so each must set its own velocity profile explicitly rather
    # than relying on whatever an earlier immediate-mode command left behind.
    programs = [
        # (label,  move_cmd,                  description)
        ("G1", f"MR {DROP_STEPS}",   f"drop / forward  ({DROP_STEPS} steps)"),
        ("G2", f"MR {ASPIRE_STEPS}", f"aspire / reverse ({ASPIRE_STEPS} steps)"),
    ]

    for label, move_cmd, desc in programs:
        print(f"  -- Program '{label}'  ({desc})")
        _pgm_or_die(sock, f"LB {label}")   # declare label
        _pgm_or_die(sock, f"VM={VELOCITY_MAX}")   # max velocity
        _pgm_or_die(sock, f"A={ACCEL}")           # acceleration
        _pgm_or_die(sock, f"D={DECEL}")           # deceleration
        _pgm_or_die(sock, move_cmd)         # motion instruction
        _pgm_or_die(sock, "H")              # hold: wait for move to finish
        _pgm_or_die(sock, "E")              # end program
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
    print(f"         G1 = drop   (MR {DROP_STEPS} steps, forward)")
    print(f"         G2 = aspire (MR {ASPIRE_STEPS} steps, reverse)")
    print(f"         VM={VELOCITY_MAX}  A={ACCEL}  D={DECEL}  (both programs)")
    print("         Programs survive power cycles.  Re-run 'upload' if step counts OR velocity change.\n")


# ── Interactive loop ──────────────────────────────────────────────────────────

_HELP = """
  Commands:
    drop    → execute G1  (forward  -1575000 steps — dispense/drop)
    aspire  → execute G2  (reverse  +1575000 steps — draw/aspire)
    pos     → print current position (no motion)
    speed   → print VM/VI/A/D — the actual current aspirate/drop velocity
              profile (no motion). Never set in this file, so this is
              whatever the motor's factory/flash default is.
    zero    → set current position as the new 0  (P=0, no motion)
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
        elif cmd == "":
            pass
        else:
            print(f"  Unknown: '{cmd}'")
            print(_HELP)


# ── Entry point ───────────────────────────────────────────────────────────────

_USAGE = """
Usage:
  python hardware_tests/linear_control.py upload    # flash G1+G2 to motor (run once)
  python hardware_tests/linear_control.py drop      # forward  -1575000 steps
  python hardware_tests/linear_control.py aspire    # reverse  +1575000 steps
  python hardware_tests/linear_control.py pos       # print position, no motion
  python hardware_tests/linear_control.py speed     # print VM/VI/A/D velocity profile, no motion
  python hardware_tests/linear_control.py run       # interactive control loop
"""

def main() -> None:
    if len(sys.argv) < 2:
        print(_USAGE)
        sys.exit(0)

    sub = sys.argv[1].lower()
    if sub not in {"upload", "drop", "aspire", "pos", "speed", "run"}:
        print(f"Unknown sub-command: '{sub}'\n{_USAGE}")
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
            elif sub == "pos":
                print_position(sock)
            elif sub == "speed":
                print_velocity(sock)
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
