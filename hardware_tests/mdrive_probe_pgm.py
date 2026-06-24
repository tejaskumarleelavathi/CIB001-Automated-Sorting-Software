"""
mdrive_probe_pgm.py — Minimal program-mode diagnostic
------------------------------------------------------
Tries the absolute simplest possible program write sequence to find out
exactly WHERE the motor rejects us.  Run this and share the full output.

Sequence tested:
  1.  Clear ER, set DE=1  (same recovery as upload)
  2.  CP + S              (wipe program space)
  3.  PG 1               (enter program mode)
  4.  MR 100             (simplest motion line — no label)
  5.  E                  (end program)
  6.  PG                 (exit program mode)
  7.  S                  (save)
  8.  EX 1               (execute program at address 1)
  9.  Poll PR MV until 0
 10.  PR P               (print position)

If step 4 (MR 100) also gets '?', then NO program writing works at all
and we have a hardware/firmware issue deeper than label syntax.

If step 4 works but LB doesn't, we'll execute programs by address
instead of by name — equally good for the syringe pump use case.
"""

import socket
import time
import sys

MDRIVE_IP       = "172.16.9.16"
MDRIVE_PORT     = 503
CMD_TIMEOUT_S   = 5.0
POLL_INTERVAL_S = 0.2
MOVE_TIMEOUT_S  = 30.0


# ── Socket helpers ────────────────────────────────────────────────────────────

def connect() -> socket.socket:
    print(f"[CONNECT] {MDRIVE_IP}:{MDRIVE_PORT} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(CMD_TIMEOUT_S)
        sock.connect((MDRIVE_IP, MDRIVE_PORT))
        print("[CONNECT] Connected.\n")
        return sock
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)


def read_prompt(sock: socket.socket) -> str:
    """Read until '>' or '?' — for immediate mode commands."""
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


def read_pgm(sock: socket.socket) -> str:
    """
    Read after sending a program-mode line.
    Success = a line containing only digits (the address counter).
    Error   = '?' appears anywhere.
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


def imm(sock: socket.socket, cmd: str) -> tuple[str, bool]:
    """Send in immediate mode, return (response, ok)."""
    print(f"  [IMM] >> {cmd}")
    sock.sendall((cmd + "\r").encode("ascii"))
    resp = read_prompt(sock)
    ok = resp.rstrip().endswith(">")
    print(f"         << {resp!r}  {'OK' if ok else 'ERROR'}")
    return resp, ok


def pgm(sock: socket.socket, cmd: str) -> tuple[str, bool]:
    """Send in program mode, return (response, ok)."""
    print(f"  [PGM] >> {cmd}")
    sock.sendall((cmd + "\r").encode("ascii"))
    resp = read_pgm(sock)
    ok = "?" not in resp
    print(f"         << {resp!r}  {'OK' if ok else 'ERROR'}")
    return resp, ok


def extract(resp: str, sent: str) -> str:
    for line in resp.splitlines():
        c = line.strip()
        if c and c != sent and c not in (">", "?") and c.lstrip("-").isdigit():
            return c
    return ""


# ── Main diagnostic ───────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  MDrive Program-Mode Probe")
    print("=" * 60)
    sock = connect()

    try:
        # ── 1. Recovery (same as upload) ──────────────────────────────────────
        print("\n[STEP 1] Recovery: clear ER, enable DE")
        imm(sock, "PR ER")
        imm(sock, "ER=0")
        imm(sock, "DE=1")
        time.sleep(0.5)
        imm(sock, "PR ER")
        imm(sock, "PR DE")

        # ── 2. Wipe program space ─────────────────────────────────────────────
        print("\n[STEP 2] Clear program space (CP + S)")
        _, ok = imm(sock, "CP")
        if not ok:
            print("  [WARN] CP returned ?")
        _, ok = imm(sock, "S")
        if not ok:
            print("  [WARN] S returned ?")

        # ── 3. Enter program mode ─────────────────────────────────────────────
        print("\n[STEP 3] Enter program mode (PG 1)")
        resp, ok = pgm(sock, "PG 1")
        if not ok:
            print("  [FATAL] Cannot enter program mode.")
            sys.exit(1)
        print("  [OK] In program mode.")

        # ── 4. Simplest possible instruction: MR 100 (no label) ──────────────
        print("\n[STEP 4] Write simplest instruction: MR 100  (no label)")
        resp_mr, ok_mr = pgm(sock, "MR 100")
        if not ok_mr:
            print("  [FATAL] Even MR 100 in program mode failed!")
            print("  This means program writing is blocked at the firmware level.")
            print("  → Exit program mode and read ER for diagnosis.")
            sock.sendall(b"PG\r"); time.sleep(0.5); sock.recv(256)
            imm(sock, "PR ER")
            sys.exit(1)

        # ── 5. Try H (hold) ───────────────────────────────────────────────────
        print("\n[STEP 5] Write H (hold until motion done)")
        pgm(sock, "H")

        # ── 6. Try E (end program) ────────────────────────────────────────────
        print("\n[STEP 6] Write E (end program)")
        pgm(sock, "E")

        # ── 7. Try LB — separate from the motion instructions ─────────────────
        # Write a SECOND program with a label, so we know LB failure
        # doesn't affect the first (label-free) program.
        print("\n[STEP 7] Now try LB with several name formats ...")
        label_candidates = ["A1", "G1", "P1", "T1", "AB", "GO"]
        working_label = None
        for candidate in label_candidates:
            print(f"\n  Trying: LB {candidate}")
            resp_lb, ok_lb = pgm(sock, f"LB {candidate}")
            if ok_lb:
                working_label = candidate
                print(f"  [OK] LB {candidate} ACCEPTED!")
                # Write minimal body and end.
                pgm(sock, "MR -100")
                pgm(sock, "H")
                pgm(sock, "E")
                break
            else:
                sock.sendall(b"PG\r"); time.sleep(0.3)
                sock.recv(256)
                imm(sock, "PR ER")
                # Re-enter program mode for next attempt
                pgm(sock, "PG 1")

        # ── 8. Exit program mode ──────────────────────────────────────────────
        print("\n[STEP 8] Exit program mode (PG)")
        imm(sock, "PG")

        # ── 9. Save ───────────────────────────────────────────────────────────
        print("\n[STEP 9] Save to flash (S)")
        imm(sock, "S")

        # ── 10. Execute label-free program at address 1 ───────────────────────
        print("\n[STEP 10] Execute program at address 1 (EX 1)")
        _, ok = imm(sock, "EX 1")
        if not ok:
            print("  [WARN] EX 1 returned ? — reading ER ...")
            imm(sock, "PR ER")
        else:
            print("  [OK] Program started.  Polling MV ...")
            deadline = time.monotonic() + MOVE_TIMEOUT_S
            while time.monotonic() < deadline:
                resp_mv, _ = imm(sock, "PR MV")
                mv = extract(resp_mv, "PR MV")
                if mv == "0":
                    print("  [OK] Motor stopped.")
                    break
                time.sleep(POLL_INTERVAL_S)
            imm(sock, "PR P")

        # ── Summary ───────────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  PROBE SUMMARY")
        print(f"  MR 100 in program mode : {'OK' if ok_mr else 'FAILED'}")
        print(f"  LB working label       : {working_label or 'NONE — all rejected'}")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n[ABORT]")
        try:
            imm(sock, "SL 0")
        except Exception:
            pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
