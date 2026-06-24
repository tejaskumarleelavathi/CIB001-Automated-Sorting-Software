# test_autonomous.py — Autonomous robot pick test
#
# Prerequisite: test_autonomous.prg must be running on the robot controller.
#
# Python sends only two commands:
#   HOME  — robot motors on, moves to P0, replies OK HOME
#   PICK  — robot does full sequence (move, ring light, pipette, sensors, home)
#           replies PICK_OK DOWN=1 UP=1  or  PICK_FAIL DOWN=x UP=x
#
# Usage: python test_autonomous.py

import socket
import sys

ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 60.0   # long — robot does full sequence at 5% speed


# ── Connection ────────────────────────────────────────────────────────────────

def connect() -> socket.socket:
    print(f"Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except OSError as e:
        print(f"FAIL: {e}")
        print("Check: test_autonomous.prg is running on RC+ (F5), robot powered on.")
        sys.exit(1)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected.\n")
    return s


def recv_line(s: socket.socket) -> str:
    """Read one response line from the robot.
    SPEL+ Print #201 sends CRLF. Read until \\n so both \\r and \\n
    are consumed — avoids leaving \\n in the buffer on the next call."""
    buf = b""
    while True:
        chunk = s.recv(1)
        if not chunk:
            raise ConnectionError("Robot closed the connection unexpectedly.")
        buf += chunk
        if buf.endswith(b"\n"):
            break
    return buf.decode(errors="replace").strip()


def send(s: socket.socket, cmd: str) -> str:
    """Send a command and return the robot's response."""
    s.sendall((cmd + "\r\n").encode())
    resp = recv_line(s)
    return resp


# ── Test ──────────────────────────────────────────────────────────────────────

def run(s: socket.socket) -> None:

    # ── HOME ──────────────────────────────────────────────────────────────────
    print("━" * 50)
    print("STEP 1 — HOME  (motors on, move to P0 — watch the robot!)")
    print("━" * 50)
    resp = send(s, "HOME")
    print(f"  Robot: {resp}")
    if "OK HOME" not in resp:
        print("  ERROR: HOME failed. Aborting.")
        return
    print("  Robot is at home position.\n")

    # ── PICK ──────────────────────────────────────────────────────────────────
    print("━" * 50)
    print("STEP 2 — PICK  (robot does full sequence autonomously)")
    print("  Sequence: move to P1 → ring light ON 5s → ring light OFF")
    print("            → pipette DOWN → check Sw(22) → pipette UP → check Sw(21)")
    print("            → return to P0 → send result")
    print("  Waiting for result (timeout = 60 s) ...")
    print("━" * 50)
    resp = send(s, "PICK")
    print(f"\n  Robot: {resp}\n")

    print("=" * 50)
    if resp.startswith("PICK_OK"):
        print("RESULT: PASS — pick sequence completed successfully")
        print(f"  {resp}")
    elif resp.startswith("PICK_FAIL"):
        print("RESULT: FAIL — one or more sensors did not confirm")
        print(f"  {resp}")
        if "DOWN=0" in resp:
            print("  Pipette DOWN sensor (Sw 22) did not trigger — check wiring/sensor")
        if "UP=0" in resp:
            print("  Pipette UP sensor (Sw 21) did not trigger — check wiring/sensor")
    else:
        print(f"RESULT: UNEXPECTED response: {resp}")
    print("=" * 50)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    s = connect()
    try:
        run(s)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"\nERROR: {e}")
    finally:
        try:
            send(s, "BYE")
        except Exception:
            pass
        s.close()
        print("\nDisconnected.")
