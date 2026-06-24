# test_robot.py -- basic connection test for Epson T3-B401S via RC+ TCP/IP
# Usage:
#   & "C:/Users/TejasKumarLeelavathi/miniconda3/envs/cib_env/python.exe" hardware_tests/test_robot.py

import socket
import time

ROBOT_IP   = "172.16.9.15"
ROBOT_PORT = 2000       # SPEL+ TCP server port (TCP/IP Port 201 in System Config)
TIMEOUT    = 5.0


def connect() -> socket.socket:
    print(f"Connecting to robot at {ROBOT_IP}:{ROBOT_PORT} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((ROBOT_IP, ROBOT_PORT))
    print("TCP connection established.")
    return s


def recv_all(s: socket.socket, wait: float = 0.5) -> str:
    time.sleep(wait)
    s.settimeout(0.5)
    chunks = []
    try:
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    except socket.timeout:
        pass
    s.settimeout(TIMEOUT)
    raw = b"".join(chunks)
    return raw.decode("ascii", errors="replace").strip()


def send(s: socket.socket, cmd: str) -> str:
    print(f"  >> {cmd}")
    s.sendall((cmd + "\r\n").encode("ascii"))
    resp = recv_all(s)
    if resp:
        print(f"  << {resp}")
    else:
        print(f"  << (no response)")
    return resp


if __name__ == "__main__":
    try:
        s = connect()
    except Exception as e:
        print(f"FAIL: Could not connect — {e}")
        print("Check: robot powered on, IP 172.16.9.15 reachable, RC+ running on controller")
        exit(1)

    with s:
        print("\n--- status ---")
        send(s, "STATUS")

        print("\n--- ring light ON (Output 12) ---")
        send(s, "OUT 12 ON")
        time.sleep(2)

        print("\n--- ring light OFF ---")
        send(s, "OUT 12 OFF")
        time.sleep(1)

        print("\n--- pipette DOWN (Output 4) ---")
        send(s, "OUT 4 ON")
        time.sleep(2)

        print("\n--- pipette UP ---")
        send(s, "OUT 4 OFF")

        send(s, "BYE")

    print("\nDone. OK = success, ERR = problem.")
