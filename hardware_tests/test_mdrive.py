"""
test_mdrive.py — Novanta Liberty MDrive (LMDAM 7.008) stepper motor
--------------------------------------------------------------------
Device:   Novanta LMDAE572C / LMDAM 7.008  (hMTechnology closed-loop)
IP:       172.16.9.16
Port:     503  (MCode/TCP — raw ASCII, NOT Modbus, NOT Telnet)

Protocol:
  - Commands sent as plain ASCII text + CR  (\\r)
  - Device echoes the command back, then sends any response data,
    then sends ">" when ready for the next command.
  - If an error is active the prompt is "?" instead of ">"  until
    the error is cleared.  Read with "PR ER", clear with "ER=0".
  - Moving status:  PR MV  →  1 = moving, 0 = stopped.
  - Position:       PR P   →  signed step count.

Motor constants (confirmed hardware):
  - 200 full steps/rev, MS = 256  →  51 200 steps per full revolution.
  - DE = 1  →  drive bridge enabled.  DE = 0  →  drive disabled (stall).
  - Recovering from a locked-rotor stall that drops DE to 0 may require
    a power cycle (or in extreme cases "FD" — factory reset, destructive).

Usage (from the CIB_Software root, inside cib_env):
    python hardware_tests/test_mdrive.py
"""

import socket
import time
import sys

# ── Connection settings ──────────────────────────────────────────────────────
MDRIVE_IP   = "172.16.9.16"
MDRIVE_PORT = 503

# Maximum seconds to wait for a single command response from the device.
CMD_TIMEOUT_S = 5.0

# How long to wait between "PR MV" polls while a move is in progress.
POLL_INTERVAL_S = 0.25

# Maximum time to wait for any single move to complete before giving up.
MOVE_TIMEOUT_S = 30.0

# Steps per revolution: 200 full steps × 256 microsteps = 51 200.
STEPS_PER_REV = 51_200


# ── Low-level socket helpers ─────────────────────────────────────────────────

def connect() -> socket.socket:
    """Open a raw TCP socket to the MDrive.  Exits on failure."""
    print(f"[CONNECT] Connecting to {MDRIVE_IP}:{MDRIVE_PORT} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(CMD_TIMEOUT_S)
        sock.connect((MDRIVE_IP, MDRIVE_PORT))
        print("[CONNECT] Connected.\n")
        return sock
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"[ERROR] Cannot connect to {MDRIVE_IP}:{MDRIVE_PORT} — {exc}")
        sys.exit(1)


def read_until_prompt(sock: socket.socket) -> str:
    """
    Read bytes from *sock* until we see a '>' or '?' prompt character,
    or until CMD_TIMEOUT_S seconds have elapsed.

    Returns the full raw response (stripped of leading/trailing whitespace).
    The caller can check whether the last character is '?' to detect errors.
    """
    buf = ""
    deadline = time.monotonic() + CMD_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(256).decode("ascii", errors="replace")
            buf += chunk
            # The MDrive ends every response with the prompt, possibly
            # preceded by CR/LF.  Stop as soon as we see either prompt.
            if ">" in buf or "?" in buf:
                break
        except socket.timeout:
            # Short recv timeout — keep looping until our deadline.
            continue
    return buf.strip()


def send_cmd(sock: socket.socket, command: str, *, verbose: bool = True) -> tuple[str, bool]:
    """
    Send *command* (a single MCode instruction) to the MDrive and read the
    full response up to the prompt character.

    Returns
    -------
    response : str
        The full text received from the device (echo + data + prompt).
    ok : bool
        True  → prompt was '>'  (no active error).
        False → prompt was '?'  (error condition, drive may be disabled).
    """
    raw = command + "\r"
    if verbose:
        print(f"  >> {command}")
    sock.sendall(raw.encode("ascii"))
    response = read_until_prompt(sock)
    if verbose:
        print(f"  << {response!r}")
    # The final non-whitespace character reveals the prompt state.
    stripped = response.rstrip()
    ok = stripped.endswith(">") if stripped else False
    return response, ok


def extract_value(response: str, command: str) -> str:
    """
    Pull the numeric answer out of a "PR <REG>" response.

    The MDrive echoes the sent command on the first line, then prints the
    value on the next line, then the prompt.  We look for a line that is
    purely numeric (allowing a leading minus sign).
    """
    for line in response.splitlines():
        line = line.strip()
        # Skip blank lines, the echoed command, and the prompt characters.
        if not line or line == command or line in (">", "?"):
            continue
        # Accept lines that look like an integer (possibly negative).
        candidate = line.replace(" ", "")
        if candidate.lstrip("-").isdigit():
            return candidate
    return ""


# ── High-level motor helpers ─────────────────────────────────────────────────

def check_firmware(sock: socket.socket) -> None:
    """Send 'PR VR' and print the firmware version — sanity-checks the link."""
    print("[STEP 1] Reading firmware version (PR VR) ...")
    resp, ok = send_cmd(sock, "PR VR")
    if not ok:
        print("[ERROR] Device responded with error prompt on PR VR — check connection.")
        sys.exit(1)
    # The version string appears on its own line between the echo and the prompt.
    lines = [l.strip() for l in resp.splitlines() if l.strip() not in ("PR VR", ">", "?", "")]
    version = lines[0] if lines else "(not found)"
    print(f"[OK]    Firmware: {version}\n")


def check_and_clear_error(sock: socket.socket) -> None:
    """
    Read ER.  If non-zero, attempt to clear it with 'ER=0'.
    Exits if the error cannot be cleared.
    """
    print("[STEP 2] Checking error register (PR ER) ...")
    resp, _ = send_cmd(sock, "PR ER")
    er_val = extract_value(resp, "PR ER")
    print(f"[INFO]  ER = {er_val!r}")

    if er_val == "0" or er_val == "":
        print("[OK]    No active errors.\n")
        return

    # Non-zero error — try to clear it.
    print(f"[WARN]  Error register is non-zero ({er_val}).  Attempting 'ER=0' ...")
    send_cmd(sock, "ER=0")

    # Re-read to confirm cleared.
    resp2, ok2 = send_cmd(sock, "PR ER")
    er_val2 = extract_value(resp2, "PR ER")
    if er_val2 != "0" or not ok2:
        print(f"[ERROR] Error register still non-zero ({er_val2}) after clear attempt.")
        print("        The drive may require a power cycle.  Exiting.")
        sys.exit(1)

    print("[OK]    Error cleared.\n")


def ensure_drive_enabled(sock: socket.socket) -> None:
    """
    Check DE (drive enable).  If 0, try to set it to 1.
    Exits if the drive cannot be enabled.
    """
    print("[STEP 3] Checking drive enable (PR DE) ...")
    resp, _ = send_cmd(sock, "PR DE")
    de_val = extract_value(resp, "PR DE")
    print(f"[INFO]  DE = {de_val!r}")

    if de_val == "1":
        print("[OK]    Drive is already enabled.\n")
        return

    print("[WARN]  Drive is disabled.  Attempting 'DE=1' ...")
    _, ok = send_cmd(sock, "DE=1")

    # Re-read to confirm.
    resp2, _ = send_cmd(sock, "PR DE")
    de_val2 = extract_value(resp2, "PR DE")
    if de_val2 != "1":
        print("[ERROR] Drive still reports DE=0 after enable attempt.")
        print("        Possible stall/locked-rotor condition.  A power cycle may be required.")
        print("        Do NOT send 'FD' (factory reset) unless you intend to wipe the device.")
        sys.exit(1)

    print("[OK]    Drive enabled.\n")


def wait_for_stop(sock: socket.socket, description: str = "move") -> None:
    """
    Poll 'PR MV' in a loop until the motor reports stopped (MV = 0).
    Raises RuntimeError if MOVE_TIMEOUT_S is exceeded.
    """
    print(f"[POLL]  Waiting for {description} to complete ...")
    deadline = time.monotonic() + MOVE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp, _ = send_cmd(sock, "PR MV", verbose=False)
        mv_val = extract_value(resp, "PR MV")
        if mv_val == "0":
            print("[OK]    Motor stopped.\n")
            return
        # Still moving — wait before polling again.
        time.sleep(POLL_INTERVAL_S)

    # Timed out — stop the motor and raise.
    print(f"[ERROR] Move timed out after {MOVE_TIMEOUT_S:.0f}s!  Sending stop command (SL 0) ...")
    send_cmd(sock, "SL 0")   # SL 0 = set velocity to 0 (stops a slew move)
    raise RuntimeError(f"Motor did not stop within {MOVE_TIMEOUT_S}s for: {description}")


def read_position(sock: socket.socket) -> int:
    """Read and return the current position counter (PR P)."""
    resp, _ = send_cmd(sock, "PR P")
    val = extract_value(resp, "PR P")
    pos = int(val) if val.lstrip("-").isdigit() else 0
    print(f"[INFO]  Position = {pos} steps  ({pos / STEPS_PER_REV:.4f} revolutions)\n")
    return pos


# ── Main test sequence ───────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  MDrive Motor Test — CIB Syringe Pump")
    print(f"  {MDRIVE_IP}:{MDRIVE_PORT}   |   {STEPS_PER_REV} steps/rev")
    print("=" * 60)
    print()

    sock = connect()

    try:
        # ── Step 1: Firmware sanity check ────────────────────────────────────
        check_firmware(sock)

        # ── Step 2: Error register ───────────────────────────────────────────
        check_and_clear_error(sock)

        # ── Step 3: Drive enable ─────────────────────────────────────────────
        ensure_drive_enabled(sock)

        # ── Step 4: 2 revolutions FORWARD ────────────────────────────────────
        steps_fwd = 2 * STEPS_PER_REV   # = 102 400
        print(f"[STEP 4] Moving FORWARD {steps_fwd} steps (2 revolutions) ...")
        _, ok = send_cmd(sock, f"MR {steps_fwd}")
        if not ok:
            print("[ERROR] Forward move command rejected (error prompt).  Check ER and DE.")
            sys.exit(1)

        # Poll until stopped.
        wait_for_stop(sock, "forward move (2 rev)")

        # Print position.
        print("[STEP 5] Reading position after forward move:")
        pos_after_fwd = read_position(sock)

        # ── Step 5: 2 revolutions REVERSE ────────────────────────────────────
        steps_rev = -2 * STEPS_PER_REV  # = -102 400
        print(f"[STEP 6] Moving REVERSE {steps_rev} steps (2 revolutions back) ...")
        _, ok = send_cmd(sock, f"MR {steps_rev}")
        if not ok:
            print("[ERROR] Reverse move command rejected (error prompt).  Check ER and DE.")
            sys.exit(1)

        wait_for_stop(sock, "reverse move (2 rev back)")

        # Print final position.
        print("[STEP 7] Reading final position:")
        pos_final = read_position(sock)

        # ── Summary ───────────────────────────────────────────────────────────
        print("=" * 60)
        print("  TEST COMPLETE")
        print(f"  Position after forward move : {pos_after_fwd} steps")
        print(f"  Final position (after return): {pos_final} steps")
        net_drift = pos_final  # started at 0 (or wherever P was before)
        print(f"  Net positional drift         : {net_drift} steps  ({net_drift / STEPS_PER_REV:.6f} rev)")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n[ABORT] Keyboard interrupt — sending stop command (SL 0) ...")
        try:
            send_cmd(sock, "SL 0", verbose=False)
        except Exception:
            pass

    except RuntimeError as exc:
        print(f"\n[FATAL] {exc}")
        sys.exit(1)

    finally:
        print("\n[CONNECT] Closing socket.")
        sock.close()


if __name__ == "__main__":
    main()
