"""
test_sort_server_home.py -- isolated hardware test for sort_server.prg's
new hand-aware HOME command (Phase 0 of the multi-station production
integration, see feature/multi-station-production-integration).

Does NOT touch cameras, PLC, or safety controller -- just connects to the
robot's normal production port (172.16.9.15:2000) the same way orchestrator.py
does, and repeatedly calls HOME so you can jog the arm between calls and
watch the behavior on RC+'s Jog & Teach / Current Position panel.

Usage:
    conda run -n cib_env python hardware_tests/test_sort_server_home.py

Make sure sort_server.prg (the updated version, with hand-aware HOME) is
downloaded and running as Main on the controller before running this.

Test sequence:
  1. Connects, sends STATUS to confirm the server is alive.
  2. Sends HOME once immediately (whatever hand it happens to be in).
  3. Then loops: prompts you to jog the arm (in RC+, motor stays on) to a
     new position/hand, press Enter, and it sends HOME again -- so you can
     watch each transition. Ctrl+C to quit.
"""

import socket
import sys

IP = "172.16.9.15"
PORT = 2000


def send_cmd(sock: socket.socket, cmd: str) -> str:
    sock.sendall((cmd + "\r\n").encode("ascii"))
    buf = b""
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Robot disconnected.")
        if chunk == b"\n":
            break
        buf += chunk
    return buf.decode("ascii", errors="replace").strip()


def main() -> None:
    print(f"Connecting to {IP}:{PORT} ...")
    sock = socket.create_connection((IP, PORT), timeout=10)
    sock.settimeout(30)
    print("Connected.")

    resp = send_cmd(sock, "STATUS")
    print(f"STATUS -> {resp!r}")
    if not resp.startswith("OK"):
        print("Server did not respond OK to STATUS -- aborting.")
        sys.exit(1)

    print("\nSending HOME (whatever hand it's currently in) ...")
    resp = send_cmd(sock, "HOME")
    print(f"HOME -> {resp!r}")
    if not resp.startswith("OK HOME"):
        print("[WARN] Unexpected HOME response -- check RC+ for an error.")

    print(
        "\nNow: in RC+'s Jog & Teach panel, jog the arm to a new position "
        "(try both Righty and Lefty postures across separate runs of this "
        "loop), then press Enter here to send HOME again and watch the "
        "transition. Ctrl+C to quit.\n"
    )
    try:
        while True:
            input("Press Enter to send HOME... ")
            resp = send_cmd(sock, "HOME")
            print(f"HOME -> {resp!r}")
            if not resp.startswith("OK HOME"):
                print("[WARN] Unexpected HOME response -- check RC+ for an error.")
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        try:
            send_cmd(sock, "BYE")
        except Exception:
            pass
        sock.close()


if __name__ == "__main__":
    main()
