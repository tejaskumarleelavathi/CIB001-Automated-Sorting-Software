# test_sequence.py — Full hardware test sequence
#
# Prerequisite: test_sequence.prg must be running on the robot controller.
#
# Sequence:
#   1. Home robot (P1)
#   2. Turn ring light ON
#   3. Wait 5 seconds
#   4. Turn ring light OFF
#   5. Move to pipette test position (P2)
#   6. Pipette DOWN (Out 4 On)
#   7. Check input 22 — print pipette down confirmed
#   8. Pipette UP (Out 4 Off)
#   9. Check input 21 — print pipette up confirmed
#  10. Return to home (P1)
#  11. Done
#
# Usage: python test_sequence.py

import socket
import time
import sys

ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT    = 30.0   # long — robot moves slowly at 5% speed


# ── Connection ────────────────────────────────────────────────────────────────

def connect() -> socket.socket:
    print(f"Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect((ROBOT_IP, ROBOT_PORT))
    except OSError as e:
        print(f"FAIL: {e}")
        print("Check: test_sequence.prg is running on RC+ (F5), robot powered on.")
        sys.exit(1)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected.\n")
    return s


def recv_line(s: socket.socket) -> str:
    """Read one response line from the robot.
    SPEL+ Print #201 sends CRLF (\r\n). Read until \n so both \r and \n
    are consumed in one call — avoids leaving \n in the buffer."""
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
    """Send a command and return the robot's response. Blocks until response arrives."""
    s.sendall((cmd + "\r\n").encode())
    resp = recv_line(s)
    return resp


# ── Sequence ──────────────────────────────────────────────────────────────────

def run_sequence(s: socket.socket) -> None:

    # ── Step 1: Home ─────────────────────────────────────────────────────────
    print("━" * 50)
    print("STEP 1 — Homing robot (speed = 5%, be ready to hit e-stop)")
    print("━" * 50)
    resp = send(s, "HOME")
    print(f"  Robot: {resp}")
    if "OK" not in resp:
        print("  ERROR: HOME failed. Aborting.")
        return
    print("  Robot is at home position.\n")
    time.sleep(1)

    # ── Step 2: Move to pipette test position ────────────────────────────────
    print("━" * 50)
    print("STEP 2 — Moving to pipette test position (P2) — watch the robot!")
    print("━" * 50)
    resp = send(s, "MOVE_TEST")
    print(f"  Robot: {resp}")
    print("  Robot is at pipette test position.\n")
    time.sleep(1)

    # ── Step 3: Ring light ON ─────────────────────────────────────────────────
    print("STEP 3 — Ring light ON")
    resp = send(s, "RING_ON")
    print(f"  Robot: {resp}")
    print("  Ring light should be ON now. Waiting 5 seconds ...\n")
    time.sleep(5)

    # ── Step 4: Ring light OFF ────────────────────────────────────────────────
    print("STEP 4 — Ring light OFF")
    resp = send(s, "RING_OFF")
    print(f"  Robot: {resp}\n")
    time.sleep(1)

    # ── Step 5: Pipette DOWN ──────────────────────────────────────────────────
    print("STEP 5 — Pipette DOWN (Out 4 On)")
    resp = send(s, "PIPETTE_DOWN")
    print(f"  Robot: {resp}")
    print("  Waiting 1 second for actuation ...")
    time.sleep(1)

    # ── Step 6: Check input 22 ────────────────────────────────────────────────
    print("\nSTEP 6 — Checking input 22 (pipette down sensor)")
    resp = send(s, "CHECK_IN 22")
    print(f"  Robot: {resp}")
    if "= 1" in resp:
        print("  PASS — INPUT 22 = 1 — PIPETTE IS DOWN confirmed")
    else:
        print("  FAIL — INPUT 22 = 0 — Pipette down NOT confirmed (check wiring / sensor)")
    time.sleep(2)

    # ── Step 7: Pipette UP ────────────────────────────────────────────────────
    print("\nSTEP 7 — Pipette UP (Out 4 Off)")
    resp = send(s, "PIPETTE_UP")
    print(f"  Robot: {resp}")
    print("  Waiting 1 second for retraction ...")
    time.sleep(1)

    # ── Step 8: Check input 21 ────────────────────────────────────────────────
    print("\nSTEP 8 — Checking input 21 (pipette up sensor)")
    resp = send(s, "CHECK_IN 21")
    print(f"  Robot: {resp}")
    if "= 1" in resp:
        print("  PASS — INPUT 21 = 1 — PIPETTE IS UP confirmed")
    else:
        print("  FAIL — INPUT 21 = 0 — Pipette up NOT confirmed (check wiring / sensor)")
    time.sleep(1)

    # ── Step 9: Return home ───────────────────────────────────────────────────
    print("\n" + "━" * 50)
    print("STEP 9 — Returning to home position (P1)")
    print("━" * 50)
    resp = send(s, "HOME")
    print(f"  Robot: {resp}")
    print("  Robot is back at home.\n")

    print("=" * 50)
    print("SEQUENCE COMPLETE")
    print("=" * 50)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    s = connect()
    try:
        run_sequence(s)
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
        print("Disconnected.")
