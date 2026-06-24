"""
test_lighting.py — DCS-100E LED controller

Device:  Advanced Illumination DCS-100E (3-channel, lighthead BL245-1212WHIC1)
IP:      172.16.9.20
Port:    777  (raw TCP, ASCII SCPI commands)

Usage:
    conda run -n cib_env python test_lighting.py on
    conda run -n cib_env python test_lighting.py off
    conda run -n cib_env python test_lighting.py status
    conda run -n cib_env python test_lighting.py all_on   ← test simultaneous activation
    conda run -n cib_env python test_lighting.py all_off
"""

import socket
import time
import sys

DCS_IP     = "172.16.9.20"
DCS_PORT   = 777
TIMEOUT    = 2.0
CHANNELS   = [1, 2, 3]
CURRENT_MA = 250


def connect() -> socket.socket:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(TIMEOUT)
        sock.connect((DCS_IP, DCS_PORT))
        return sock
    except (ConnectionRefusedError, OSError) as e:
        print(f"[ERROR] Cannot connect to {DCS_IP}:{DCS_PORT} — {e}")
        sys.exit(1)


def send_batch(sock: socket.socket, commands: list[str]) -> str:
    """Send all commands in one shot, read all responses together."""
    payload = "".join(
        cmd if cmd.endswith(";") else cmd + ";"
        for cmd in commands
    )
    print(f"  >> {payload}")
    sock.sendall(payload.encode("ascii"))
    time.sleep(0.05)   # one wait for all responses
    try:
        resp = sock.recv(4096).decode("ascii").strip()
    except socket.timeout:
        resp = ""
    if resp:
        print(f"  << {resp}")
    return resp


def send(sock: socket.socket, command: str) -> str:
    return send_batch(sock, [command])


def lights_on(sock: socket.socket) -> None:
    print(f"Turning ON all channels at {CURRENT_MA} mA ...")
    cmds = []
    for ch in CHANNELS:
        cmds.append(f"SET:MODE:CHANNEL{ch},1")
        cmds.append(f"SET:LEVEL:CHANNEL{ch},{CURRENT_MA}")
    send_batch(sock, cmds)
    print("Done — backlight ON.")


def lights_off(sock: socket.socket) -> None:
    print("Turning OFF all channels ...")
    cmds = [f"SET:MODE:CHANNEL{ch},0" for ch in CHANNELS]
    send_batch(sock, cmds)
    print("Done — backlight OFF.")


def lights_all_on(sock: socket.socket) -> None:
    """Test SET:MODE:ALL — fires all channels simultaneously if device supports it."""
    print("Testing simultaneous ON via SET:MODE:ALL ...")
    resp = send(sock, "SET:MODE:ALL,1")
    print(f"  Response: {repr(resp)}")
    print("  Watch: did all 3 channels light up at the same instant?")
    print("  If device returned an error or nothing happened, ALL is not supported.")

def lights_all_off(sock: socket.socket) -> None:
    """Test SET:MODE:ALL off."""
    print("Testing simultaneous OFF via SET:MODE:ALL ...")
    resp = send(sock, "SET:MODE:ALL,0")
    print(f"  Response: {repr(resp)}")

def status(sock: socket.socket) -> None:
    send(sock, "*IDN?")
    send(sock, "*CHANNEL:CONFIGS?")


if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "on"

    sock = connect()
    with sock:
        if cmd == "on":
            lights_on(sock)
        elif cmd == "off":
            lights_off(sock)
        elif cmd == "all_on":
            lights_all_on(sock)
        elif cmd == "all_off":
            lights_all_off(sock)
        elif cmd == "status":
            status(sock)
        else:
            print(f"Unknown: {cmd}. Use: on | off | all_on | all_off | status")
            sys.exit(1)
        send(sock, "DISCONNECT")
