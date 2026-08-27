"""
test_sort_server_phase1.py -- isolated hardware test for Phase 1 of the
multi-station production integration (feature/multi-station-production-integration):
GO_HOME_LEFT, and the new 4-field (x,y,u,hand) form of MOVE_IMAGE_U/PICK_U/DROP_U
in sort_server.prg.

Does NOT touch cameras, PLC, or safety controller -- connects to the robot's
normal production port (172.16.9.15:2000) directly, same as orchestrator.py,
and gives you a plain command REPL so you can send exact commands and watch
RC+'s Current Position/Current Arm Orientation panel after each one.

Usage:
    conda run -n cib_env python hardware_tests/test_sort_server_phase1.py

Make sure the updated sort_server.prg (with GO_HOME_LEFT and the 4-field
_U commands) is downloaded and running as Main on the controller first.

At the prompt, type any raw command sort_server.prg understands, e.g.:
    STATUS
    HOME
    GO_HOME_LEFT
    MOVE_IMAGE_U 0,0,177.718,L
    PICK_U <x>,<y>,<u>,L        <- CAUTION: real aspirate, only send at a
                                   real dish coordinate you intend to pick from
    DROP_U <x>,<y>,<u>,L        <- CAUTION: real dispense
    BYE

Type 'quit' to exit this script (sends BYE first).
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

    print(
        "\nReady. Type a raw command (STATUS / HOME / GO_HOME_LEFT / "
        "MOVE_IMAGE_U x,y,u,hand / PICK_U x,y,u,hand / DROP_U x,y,u,hand / "
        "BYE), or 'quit' to exit.\n"
    )
    try:
        while True:
            cmd = input(">>> ").strip()
            if not cmd:
                continue
            if cmd.lower() == "quit":
                break
            try:
                resp = send_cmd(sock, cmd)
                print(f"<<< {resp!r}")
                if not resp.startswith("OK"):
                    print("[WARN] Non-OK response -- check RC+ for an error.")
            except ConnectionError as exc:
                print(f"[ERROR] {exc}")
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            send_cmd(sock, "BYE")
        except Exception:
            pass
        sock.close()
        print("Closed.")


if __name__ == "__main__":
    main()
