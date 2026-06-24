# probe_flexi.py -- figure out what protocol the Sick Flexi speaks
# Tries ping, then common industrial ports

import socket
import subprocess
import struct

FLEXI_IP = "172.16.9.12"

PORTS = {
    44818: "EtherNet/IP (CIP)",
    2222:  "EtherNet/IP (alt)",
    502:   "Modbus TCP",
    102:   "S7 / PROFINET",
    9600:  "generic TCP",
    80:    "HTTP (web UI)",
    443:   "HTTPS",
}


def ping():
    r = subprocess.run(["ping", "-n", "2", "-w", "1000", FLEXI_IP],
                       capture_output=True)
    ok = r.returncode == 0
    print(f"  Ping {FLEXI_IP}: {'REACHABLE' if ok else 'NO RESPONSE'}")
    return ok


def scan_ports():
    print(f"\n  Scanning ports on {FLEXI_IP} ...")
    open_ports = []
    for port, label in PORTS.items():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((FLEXI_IP, port))
            s.close()
            print(f"    PORT {port:5d}  OPEN   {label}")
            open_ports.append(port)
        except Exception:
            print(f"    PORT {port:5d}  closed {label}")
    return open_ports


def try_modbus():
    print(f"\n  Modbus TCP scan on port 502 ...")
    from pymodbus.client import ModbusTcpClient
    c = ModbusTcpClient(FLEXI_IP, port=502)
    if not c.connect():
        print("    Could not connect")
        return

    # Try holding registers in blocks
    print("\n  -- Holding Registers (FC3) --")
    for start in [0, 100, 200, 400, 1000, 2000, 4096, 8192]:
        r = c.read_holding_registers(address=start, count=8)
        if not r.isError():
            print(f"    regs[{start}-{start+7}] = {r.registers}")
        else:
            print(f"    regs[{start}+8]: {r}")

    # Try coils in blocks
    print("\n  -- Coils (FC1) --")
    for start in [0, 100, 200, 400, 1000, 2000, 4096, 8192]:
        r = c.read_coils(address=start, count=16)
        if not r.isError():
            vals = [int(b) for b in r.bits[:16]]
            print(f"    coils[{start}-{start+15}] = {vals}")
        else:
            print(f"    coils[{start}+16]: {r}")

    # Try input registers in blocks
    print("\n  -- Input Registers (FC4) --")
    for start in [0, 100, 200, 400, 1000, 2000, 4096, 8192]:
        r = c.read_input_registers(address=start, count=8)
        if not r.isError():
            print(f"    inregs[{start}-{start+7}] = {r.registers}")
        else:
            print(f"    inregs[{start}+8]: {r}")

    c.close()


def try_enip_raw():
    """Send a raw EtherNet/IP List Identity request — works even without pycomm3."""
    print(f"\n  Trying raw EtherNet/IP List Identity on port 44818 ...")
    LIST_IDENTITY = struct.pack("<HHIIQHH",
        0x0063,  # command: List Identity
        0,       # length: 0
        0,       # session handle
        0,       # status
        0,       # sender context (8 bytes - split across two fields)
        0,       # options
        0,
    )
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((FLEXI_IP, 44818))
        s.sendall(LIST_IDENTITY)
        data = s.recv(256)
        s.close()
        if data:
            print(f"    Got {len(data)} bytes — EtherNet/IP device responded!")
            # Try to extract product name (ASCII, usually near end of response)
            try:
                name = data[36:].decode("ascii", errors="replace").split("\x00")[0]
                print(f"    Device identity data (raw): {data.hex()}")
                print(f"    Possible name field: {name!r}")
            except Exception:
                print(f"    Raw response: {data.hex()}")
        else:
            print("    No data received")
    except Exception as e:
        print(f"    EtherNet/IP raw probe failed: {e}")


if __name__ == "__main__":
    print(f"=== Probing Sick Flexi at {FLEXI_IP} ===\n")
    reachable = ping()
    if not reachable:
        print("\n  Device not responding to ping.")
        print("  Check: Flexi powered on, Ethernet cable connected, IP correct.")
    else:
        open_ports = scan_ports()
        if 502 in open_ports:
            try_modbus()
        if 44818 in open_ports or True:   # always try raw EtherNet/IP
            try_enip_raw()
    print("\n=== Done ===")
