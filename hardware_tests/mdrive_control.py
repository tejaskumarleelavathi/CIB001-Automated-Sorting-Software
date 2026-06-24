"""
mdrive_control.py — Onboard MCode program upload + interactive control
----------------------------------------------------------------------
Device:  Novanta Liberty MDrive  (LMDAM 7.008)
IP:      172.16.9.16
Port:    503  (raw TCP ASCII, CR-terminated)

This script does TWO things depending on the sub-command:

  UPLOAD (run once, or any time you want to re-flash the programs):
      python hardware_tests/mdrive_control.py upload

      Writes three MCode programs into the motor's non-volatile flash:
        CW  — one full revolution clockwise      (MR +51200)
        CC  — one full revolution counter-clock  (MR -51200)
        HM  — absolute move back to position 0   (MA 0)

      After upload the programs survive power cycles.  You only need
      to run "upload" again if you change STEPS_PER_MOVE below.

  CONTROL (interactive or one-shot):
      python hardware_tests/mdrive_control.py cw      # one CW move
      python hardware_tests/mdrive_control.py ccw     # one CCW move
      python hardware_tests/mdrive_control.py home    # return to position 0
      python hardware_tests/mdrive_control.py pos     # print current position
      python hardware_tests/mdrive_control.py run     # interactive loop

  In "run" mode, type:
      cw    → clockwise move
      ccw   → counter-clockwise move
      home  → return to position 0
      pos   → print current position (no motion)
      zero  → set current position as the new 0 (P=0, no motion)
      q     → quit

Motor constants:
  200 full steps/rev × 256 microsteps = 51 200 steps per revolution.
  STEPS_PER_MOVE below controls how far each cw/ccw command moves.
"""

import socket
import time
import sys

# ── Settings ──────────────────────────────────────────────────────────────────
MDRIVE_IP      = "172.16.9.16"
MDRIVE_PORT    = 503

# Steps for one CW or CCW command.  Default = 1 full revolution.
STEPS_PER_MOVE = 51_200     # 200 full steps × 256 microsteps

CMD_TIMEOUT_S  = 5.0        # max wait for any single command response
MOVE_TIMEOUT_S = 30.0       # max wait for a move to complete
POLL_INTERVAL_S = 0.2       # how often to poll MV while moving


# ── Socket helpers (identical pattern to test_mdrive.py) ─────────────────────

def connect() -> socket.socket:
    """Open a raw TCP connection to the MDrive.  Exits on failure."""
    print(f"[CONNECT] {MDRIVE_IP}:{MDRIVE_PORT} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(CMD_TIMEOUT_S)
        sock.connect((MDRIVE_IP, MDRIVE_PORT))
        print("[CONNECT] Connected.\n")
        return sock
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"[ERROR] Cannot connect — {exc}")
        sys.exit(1)


def read_until_prompt(sock: socket.socket) -> str:
    """
    Read bytes until a '>' or '?' prompt is received, or CMD_TIMEOUT_S
    elapses.  Returns the full accumulated response stripped of surrounding
    whitespace.
    """
    buf = ""
    deadline = time.monotonic() + CMD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(256).decode("ascii", errors="replace")
            buf += chunk
            if ">" in buf or "?" in buf:
                break
        except socket.timeout:
            continue
    return buf.strip()


def send_pgm_line(sock: socket.socket, command: str) -> tuple[str, bool]:
    """
    Send one line of MCode while IN program mode.

    In program mode the motor NEVER sends a '>' prompt.  Instead it echoes
    the command and responds with the next available program address (a bare
    number like '  4').  Errors still produce '?'.

    We wait for either:
      - A line whose stripped content is purely digits  → success (address)
      - A '?' character anywhere in the response        → error
    """
    print(f"  >> {command}")
    sock.sendall((command + "\r").encode("ascii"))

    buf = ""
    deadline = time.monotonic() + CMD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(256).decode("ascii", errors="replace")
            buf += chunk
            if "?" in buf:
                print(f"  << {buf.strip()!r}  [?-ERROR]")
                return buf.strip(), False
            for line in buf.splitlines():
                if line.strip().isdigit():      # bare address number = success
                    print(f"  << {buf.strip()!r}")
                    return buf.strip(), True
        except socket.timeout:
            continue

    # Timeout with no recognisable response.
    print(f"  << {buf.strip()!r}  [TIMEOUT — treating as ok]")
    return buf.strip(), True   # assume ok; motor may just be slow


def send_cmd(sock: socket.socket, command: str, verbose: bool = True) -> tuple[str, bool]:
    """
    Send *command* + CR, read until prompt.

    Returns (raw_response, ok) where ok=True means '>' prompt (no error).
    """
    if verbose:
        print(f"  >> {command}")
    sock.sendall((command + "\r").encode("ascii"))
    resp = read_until_prompt(sock)
    if verbose:
        print(f"  << {resp!r}")
    ok = resp.rstrip().endswith(">")
    return resp, ok


def extract_value(resp: str, sent_cmd: str) -> str:
    """
    Pull the numeric answer out of a PR response.
    The MDrive echoes the command, then the value, then the prompt.
    """
    for line in resp.splitlines():
        line = line.strip()
        if not line or line == sent_cmd or line in (">", "?"):
            continue
        candidate = line.replace(" ", "")
        if candidate.lstrip("-").isdigit():
            return candidate
    return ""


# ── Startup checks (error + drive enable) ────────────────────────────────────

def startup_checks(sock: socket.socket) -> None:
    """Clear any error and ensure drive is enabled before moving."""

    # --- Error register ---
    resp, _ = send_cmd(sock, "PR ER", verbose=False)
    er = extract_value(resp, "PR ER")
    if er not in ("0", ""):
        print(f"[WARN] Error register = {er}.  Clearing ...")
        send_cmd(sock, "ER=0", verbose=False)
        resp2, _ = send_cmd(sock, "PR ER", verbose=False)
        er2 = extract_value(resp2, "PR ER")
        if er2 != "0":
            print(f"[ERROR] Cannot clear error register ({er2}).  Power cycle may be needed.")
            sys.exit(1)
        print("[OK]   Error cleared.")

    # --- Drive enable ---
    resp, _ = send_cmd(sock, "PR DE", verbose=False)
    de = extract_value(resp, "PR DE")
    if de != "1":
        print("[WARN] Drive disabled (DE=0).  Enabling ...")
        send_cmd(sock, "DE=1", verbose=False)
        resp2, _ = send_cmd(sock, "PR DE", verbose=False)
        de2 = extract_value(resp2, "PR DE")
        if de2 != "1":
            print("[ERROR] Cannot enable drive.  Possible stall — power cycle may be needed.")
            sys.exit(1)
        print("[OK]   Drive enabled.")


# ── Motion helpers ────────────────────────────────────────────────────────────

def wait_for_stop(sock: socket.socket, label: str = "move") -> None:
    """Poll PR MV until 0 (stopped) or timeout."""
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp, _ = send_cmd(sock, "PR MV", verbose=False)
        if extract_value(resp, "PR MV") == "0":
            return
        time.sleep(POLL_INTERVAL_S)
    print(f"[ERROR] '{label}' timed out after {MOVE_TIMEOUT_S:.0f}s.  Stopping motor.")
    send_cmd(sock, "SL 0", verbose=False)
    raise RuntimeError(f"Move timeout: {label}")


def print_position(sock: socket.socket) -> int:
    """Read and display the current position counter."""
    resp, _ = send_cmd(sock, "PR P", verbose=False)
    val = extract_value(resp, "PR P")
    pos = int(val) if val.lstrip("-").isdigit() else 0
    revs = pos / STEPS_PER_MOVE
    print(f"[POS]  {pos} steps  ({revs:+.4f} revolutions from zero)")
    return pos


def run_program(sock: socket.socket, prog_name: str, label: str) -> None:
    """Execute a named onboard program and wait for it to finish."""
    print(f"[RUN]  EX {prog_name}  ({label}) ...")
    _, ok = send_cmd(sock, f"EX {prog_name}")
    if not ok:
        print(f"[ERROR] Program '{prog_name}' rejected (? prompt).  Check ER.")
        return
    wait_for_stop(sock, label)
    print_position(sock)


# ── Upload ────────────────────────────────────────────────────────────────────

def _send_or_die(sock: socket.socket, command: str) -> str:
    """Send a command and exit if the motor responds with '?' (error)."""
    resp, ok = send_cmd(sock, command)
    if not ok:
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] '{command}' was rejected (? prompt).  ER = {er}")
        _print_er_meaning(er)
        # Try to exit program mode cleanly before dying, so the motor
        # doesn't get stuck in a half-written program state.
        try:
            sock.sendall(b"PG\r")
            time.sleep(0.3)
        except Exception:
            pass
        sys.exit(1)
    return resp


def _print_er_meaning(er: str) -> None:
    """Print a human-readable explanation for common MDrive error codes."""
    meanings = {
        "0":  "No error.",
        "8":  "Illegal data / syntax error — label name may be a reserved word.",
        "16": "I/O error.",
        "24": "Illegal Data Entered — label name or command syntax rejected by firmware.",
        "86": "Stall detected — drive bridge disabled (DE→0).",
        "25": "Position error exceeded — closed-loop following error.",
        "30": "Encoder communication fault.",
        "32": "Over-temperature.",
        "64": "Bad label / label already exists — try a different name or power-cycle to clear flash.",
        "86": "Program checksum error.",
    }
    msg = meanings.get(er, f"Unknown error code {er} — check MDrive MCode reference.")
    print(f"        ER {er} meaning: {msg}")
    if er == "8":
        print("        Suggestion: the label name may clash with a built-in command.")
        print("        Try renaming FW→F1, RV→R1, ZR→Z1 in LABEL_NAMES at top of script.")
    if er == "64":
        print("        Suggestion: a program with this name already exists on the motor.")
        print("        Power-cycle the motor to clear flash, then run upload again.")


def upload_programs(sock: socket.socket) -> None:
    """
    Write three MCode programs into the motor's non-volatile flash.

    Programs defined:
      CW  →  MR +STEPS_PER_MOVE   (clockwise)
      CC  →  MR -STEPS_PER_MOVE   (counter-clockwise)
      HM  →  MA 0                  (return to position 0)

    The sequence sent over TCP:
      LB CW          ← begin program "CW"
      MR 51200       ← one move command
      E              ← end program
      ... (repeat for CC and HM)
      S              ← save all programs to flash (non-volatile)
    """
    steps = STEPS_PER_MOVE

    # ── Step A: Always wipe program space first ───────────────────────────────
    # Previous failed upload attempts may have left partial/corrupt program
    # data in flash.  CP clears ALL programs; S commits that to NV memory.
    # This also clears LK=1 (program lock) if it was set.
    print("[UPLOAD] Checking program lock (PR LK) ...")
    resp_lk, _ = send_cmd(sock, "PR LK")
    lk = extract_value(resp_lk, "PR LK")
    print(f"[INFO]  LK = {lk!r}")
    if lk == "1":
        print("[WARN]  Program space is LOCKED — CP+S will clear it.")

    print("[UPLOAD] Clearing program space (CP) to ensure a clean slate ...")
    _send_or_die(sock, "CP")
    _send_or_die(sock, "S")
    print("[OK]   Program space cleared and saved.\n")

    # ── Step B: Enter program mode ────────────────────────────────────────────
    # LB can ONLY be used inside program mode.  'PG <address>' enters it.
    # In program mode the motor echoes a bare line-number (e.g. "  1") after
    # each line — NOT a '>' prompt.  We use send_pgm_line() for everything
    # inside program mode, and send_cmd() / _send_or_die() outside.
    print("[UPLOAD] Entering program mode (PG 1) ...")
    resp_pg, ok_pg = send_pgm_line(sock, "PG 1")
    if not ok_pg:
        resp_er, _ = send_cmd(sock, "PR ER")
        er = extract_value(resp_er, "PR ER")
        print(f"[ERROR] Could not enter program mode.  ER = {er}")
        _print_er_meaning(er)
        sys.exit(1)
    print("[OK]   In program mode.\n")

    # ── Step C: Write programs ────────────────────────────────────────────────
    # Label rules (MCode manual):
    #   - Exactly 2 characters; first = alpha, second = alpha or digit 0-9
    #   - Cannot clash with a reserved MCode mnemonic
    #   - "SU" auto-runs on power-up — never use it here
    #
    # H (Hold) inside each program waits for motion to complete before E
    # (End).  Without H the program exits while the motor is still moving.
    # Label names confirmed safe from probe testing (mdrive_probe_pgm.py):
    #   - 'A' prefix works: A1, A2, A3 all follow the same pattern
    #   - 'F' is RESERVED in this firmware (triggers error 24) — never use F*
    #   - 'R' may also be reserved (RT = return command) — avoid R*
    programs = [
        # (label_name, [mcode_body_lines], description)
        ("A1", [f"MR {steps}", "H"], f"clockwise {steps} steps = 1 rev"),
        ("A2", [f"MR -{steps}", "H"], f"counter-clockwise {steps} steps = 1 rev"),
        ("A3", ["MA 0",         "H"], "return to absolute position 0"),
    ]

    def pgm_send_or_die(cmd: str) -> None:
        """Send one program-mode line; exit with diagnostics if rejected."""
        _, ok = send_pgm_line(sock, cmd)
        if not ok:
            # Exit program mode first so we can run PR ER in immediate mode.
            print(f"[ERROR] '{cmd}' rejected inside program mode — exiting pgm mode to read ER ...")
            try:
                sock.sendall(b"PG\r")
                time.sleep(0.5)
                sock.recv(256)   # drain the PG exit response
            except Exception:
                pass
            resp_er, _ = send_cmd(sock, "PR ER")
            er = extract_value(resp_er, "PR ER")
            print(f"[ERROR] ER after failure = {er}")
            _print_er_meaning(er)
            sys.exit(1)

    print("[UPLOAD] Writing programs ...\n")
    for name, body_lines, desc in programs:
        print(f"  -- Program '{name}'  ({desc})")
        pgm_send_or_die(f"LB {name}")
        for line in body_lines:
            pgm_send_or_die(line)
        pgm_send_or_die("E")
        print()

    # ── Step D: Exit program mode ─────────────────────────────────────────────
    # 'PG' alone (no address) exits program mode.  The motor returns to
    # immediate mode and resumes sending '>' prompts.
    print("[UPLOAD] Exiting program mode (PG) ...")
    _send_or_die(sock, "PG")
    print("[OK]   Back in immediate mode.\n")

    # ── Step E: Save to non-volatile flash ────────────────────────────────────
    print("[UPLOAD] Saving to flash (S) ...")
    _send_or_die(sock, "S")

    print("\n[UPLOAD] Done.  Programs A1 (CW), A2 (CCW), A3 (home) stored on motor.")
    print(f"         {STEPS_PER_MOVE} steps/move ({STEPS_PER_MOVE // 51200} revolution(s)).")
    print("         Survives power cycles.  Re-run 'upload' only if STEPS_PER_MOVE changes.\n")


# ── Interactive loop ──────────────────────────────────────────────────────────

COMMANDS_HELP = """
  Commands:
    cw    → clockwise move (1 revolution)
    ccw   → counter-clockwise move (1 revolution)
    home  → move back to position 0
    pos   → show current position (no motion)
    zero  → redefine current position as 0  (P=0, no motion)
    q     → quit
"""

def interactive_loop(sock: socket.socket) -> None:
    print("[RUN] Interactive mode.  Type a command, then Enter.")
    print(COMMANDS_HELP)
    while True:
        try:
            cmd = input("mdrive> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[QUIT]")
            break

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "cw":
            run_program(sock, "A1", "clockwise")
        elif cmd in ("ccw", "acw"):
            run_program(sock, "A2", "counter-clockwise")
        elif cmd == "home":
            run_program(sock, "A3", "return to position 0")
        elif cmd == "pos":
            print_position(sock)
        elif cmd == "zero":
            # Redefine the current physical position as step counter = 0.
            # Next 'home' command will return here.
            _, ok = send_cmd(sock, "P=0")
            if ok:
                print("[OK]  Current position is now defined as 0.")
            else:
                print("[ERROR] Could not set P=0.")
        elif cmd == "":
            pass   # ignore empty input
        else:
            print(f"  Unknown command: '{cmd}'")
            print(COMMANDS_HELP)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Usage:
  python hardware_tests/mdrive_control.py upload   # flash programs (run once)
  python hardware_tests/mdrive_control.py cw       # one clockwise move
  python hardware_tests/mdrive_control.py ccw      # one counter-clockwise move
  python hardware_tests/mdrive_control.py home     # return to position 0
  python hardware_tests/mdrive_control.py pos      # print position, no motion
  python hardware_tests/mdrive_control.py run      # interactive control loop
"""

def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    sub = sys.argv[1].lower()
    valid = {"upload", "cw", "ccw", "acw", "home", "pos", "run"}
    if sub not in valid:
        print(f"Unknown sub-command: '{sub}'\n{USAGE}")
        sys.exit(1)

    sock = connect()

    try:
        if sub == "upload":
            # Confirm we can talk to the device.
            resp, _ = send_cmd(sock, "PR VR")
            if "LMDAM" not in resp and ">" not in resp:
                print("[ERROR] Device not responding correctly.  Check connection.")
                sys.exit(1)

            # After a stall (error 24) the drive bridge disables (DE→0) and
            # the motor re-flags an error on the very next command even after
            # ER=0.  Recovery sequence:
            #   1. Read ER so the user can see what happened.
            #   2. Clear ER=0.
            #   3. Re-enable the drive bridge: DE=1.
            #   4. Wait 500 ms for the drive to stabilise.
            #   5. Re-check ER — must stay at 0 before we touch flash.
            print("[UPLOAD] Recovering drive before flash ...")

            resp_er, _ = send_cmd(sock, "PR ER")
            er_before = extract_value(resp_er, "PR ER")
            print(f"[INFO]  ER before clear = {er_before}")

            send_cmd(sock, "ER=0")
            send_cmd(sock, "DE=1")          # re-enable bridge after stall

            print("[INFO]  Waiting 500 ms for drive to stabilise ...")
            time.sleep(0.5)

            resp2, ok2 = send_cmd(sock, "PR ER")
            er = extract_value(resp2, "PR ER")
            if er not in ("0", "") or not ok2:
                print(f"[ERROR] Error register still non-zero ({er}) after recovery.")
                print("        The motor may need a power cycle before upload will work.")
                sys.exit(1)

            resp3, _ = send_cmd(sock, "PR DE")
            de = extract_value(resp3, "PR DE")
            if de != "1":
                print(f"[ERROR] Drive still disabled (DE={de}) after DE=1 command.")
                print("        The motor may need a power cycle.")
                sys.exit(1)

            print("[OK]   Drive recovered (ER=0, DE=1).  Proceeding with upload.\n")

            upload_programs(sock)

        else:
            # All motion sub-commands: check errors + drive enable first.
            startup_checks(sock)
            print()

            if sub == "cw":
                run_program(sock, "A1", "clockwise")
            elif sub in ("ccw", "acw"):
                run_program(sock, "A2", "counter-clockwise")
            elif sub == "home":
                run_program(sock, "A3", "return to position 0")
            elif sub == "pos":
                print_position(sock)
            elif sub == "run":
                interactive_loop(sock)

    except KeyboardInterrupt:
        print("\n[ABORT] Ctrl+C — stopping motor ...")
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
