"""
test_robot_motor_direct.py
--------------------------
Test that the robot can command the linear motor directly over TCP.

Communication chain:
    This laptop -> Robot (172.16.9.15:2000) -> Motor (172.16.9.60:503)

The robot program test_motor_comms.prg must be loaded and running in RC+
before you run this script (NOT sort_server.prg — load test_motor_comms.prg).

Usage:
    python hardware_tests/test_robot_motor_direct.py aspire
    python hardware_tests/test_robot_motor_direct.py drop
    python hardware_tests/test_robot_motor_direct.py both     # aspire then drop
"""

import socket
import sys
import time

ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000
TIMEOUT_S  = 60.0   # motor move takes up to 20s + robot comms overhead


def connect() -> socket.socket:
    print(f"[CONNECT] Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        sock.connect((ROBOT_IP, ROBOT_PORT))
        print("[CONNECT] Connected.\n")
        return sock
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        print(f"[ERROR] Cannot connect to robot: {exc}")
        print("        Make sure test_motor_comms.prg is loaded and running in RC+ (F5).")
        sys.exit(1)


def send(sock: socket.socket, cmd: str) -> str:
    print(f"  >> {cmd}")
    sock.sendall((cmd + "\r\n").encode("ascii"))
    buf = b""
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Robot closed connection unexpectedly")
        buf += chunk
        if buf.endswith(b"\n"):
            break
    resp = buf.decode("ascii", errors="replace").strip()
    print(f"  << {resp}")
    return resp


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1].lower() not in ("aspire", "drop", "both"):
        print(__doc__)
        sys.exit(0)

    sub = sys.argv[1].lower()

    sock = connect()

    try:
        if sub in ("aspire", "both"):
            print("[TEST] Sending ASPIRE — robot will connect to motor and draw back ...")
            resp = send(sock, "ASPIRE")
            if "OK ASPIRATED" in resp:
                print("[PASS] Aspire complete.\n")
            else:
                print(f"[FAIL] Unexpected response: {resp!r}\n")

        if sub == "both":
            print("Waiting 2 seconds before drop ...")
            time.sleep(2)

        if sub in ("drop", "both"):
            print("[TEST] Sending DROP — robot will connect to motor and push forward ...")
            resp = send(sock, "DROP")
            if "OK DROPPED" in resp:
                print("[PASS] Drop complete.\n")
            else:
                print(f"[FAIL] Unexpected response: {resp!r}\n")

        send(sock, "BYE")

    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
    finally:
        sock.close()
        print("[CONNECT] Disconnected.")


if __name__ == "__main__":
    main()
