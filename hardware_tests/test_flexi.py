# test_flexi.py -- Sick Flexi Compact safety controller via Modbus TCP
#
# Device:   Sick Flexi Compact @ 172.16.9.12, port 502
# Protocol: Modbus TCP, FC4 (Input Registers)
#
# ADDRESS MAP (confirmed by research 2026-06-18):
#   Address 2000-2007 = Device STATUS/METADATA block (STATIC — reg2000=0x0003 = "Run" mode)
#   Address 256       = Live PROCESS I/O block (25 registers = 25 words of live signal data)
#
# Address 2000 value 0x0003 is correct and expected — it means the Flexi is in "Run" mode.
# The process I/O data (inputs, outputs) is at address 256.
#
# REGISTER MAP at address 256 (best-guess — verify with 'raw' mode + physical triggers):
#   Word 0 (reg 256) bits from field wiring in David's Safety Designer project.
#   Run 'python test_flexi.py raw' then trigger e-stop / light curtain to find which bits flip.
#
# Usage:
#   python test_flexi.py status        -- decode live I/O once
#   python test_flexi.py watch         -- live poll every 0.5s (Ctrl+C to stop)
#   python test_flexi.py raw           -- dump all 25 process I/O registers in hex (one shot)
#   python test_flexi.py rawwatch      -- live dump all 25 registers (Ctrl+C to stop)
#   python test_flexi.py meta          -- read status/metadata block (address 2000)
#   python test_flexi.py reset_pstop   -- pulse P-Stop reset bit (address TBC)

import sys
import time
from pymodbus.client import ModbusTcpClient

FLEXI_IP   = "172.16.9.12"
FLEXI_PORT = 502
UNIT_ID    = 1

# Process I/O live data block (read with FC4)
PROC_ADDR  = 256
PROC_COUNT = 25

# Device metadata block (static — confirms Run mode)
META_ADDR  = 2000
META_COUNT = 8

# Output write address (TBC — ask David for actual holding register address)
REG_OUT    = 256


def plc() -> ModbusTcpClient:
    c = ModbusTcpClient(FLEXI_IP, port=FLEXI_PORT)
    if not c.connect():
        print(f"ERROR: Cannot connect to Flexi at {FLEXI_IP}:{FLEXI_PORT}")
        sys.exit(1)
    return c


def read_process(c: ModbusTcpClient) -> list[int]:
    """Read 25 live process I/O registers from address 256."""
    r = c.read_input_registers(address=PROC_ADDR, count=PROC_COUNT)
    if r.isError():
        raise RuntimeError(f"Read failed at address {PROC_ADDR}: {r}")
    return r.registers


def read_meta(c: ModbusTcpClient) -> list[int]:
    """Read 8 device metadata registers from address 2000."""
    r = c.read_input_registers(address=META_ADDR, count=META_COUNT)
    if r.isError():
        raise RuntimeError(f"Read failed at address {META_ADDR}: {r}")
    return r.registers


def decode(regs: list[int]) -> None:
    """
    Best-guess decode of Sick Flexi Compact process I/O.
    Word 0 and Word 1 carry the first 32 input signal bits.
    Run 'rawwatch' mode and physically trigger each sensor to confirm
    which bit positions correspond to which field devices.
    """
    w0 = regs[0]
    w1 = regs[1] if len(regs) > 1 else 0

    # Big-endian split: high byte = upper 8 bits, low byte = lower 8 bits
    b0_hi = (w0 >> 8) & 0xFF   # word 0 high byte
    b0_lo = w0 & 0xFF           # word 0 low byte
    b1_hi = (w1 >> 8) & 0xFF   # word 1 high byte
    b1_lo = w1 & 0xFF           # word 1 low byte

    def hi(byte, n): return bool(byte & (1 << n))

    print(f"  Raw word[0] = 0x{w0:04X}  (hi=0x{b0_hi:02X} lo=0x{b0_lo:02X})")
    print(f"  Raw word[1] = 0x{w1:04X}  (hi=0x{b1_hi:02X} lo=0x{b1_lo:02X})")
    print()
    print("  BEST-GUESS DECODE (verify by triggering each sensor in rawwatch mode):")
    print(f"    word[0] bit 0  : {'1' if hi(w0, 0)  else '0'}")
    print(f"    word[0] bit 1  : {'1' if hi(w0, 1)  else '0'}")
    print(f"    word[0] bit 2  : {'1' if hi(w0, 2)  else '0'}")
    print(f"    word[0] bit 3  : {'1' if hi(w0, 3)  else '0'}")
    print(f"    word[0] bit 4  : {'1' if hi(w0, 4)  else '0'}")
    print(f"    word[0] bit 5  : {'1' if hi(w0, 5)  else '0'}")
    print(f"    word[0] bit 6  : {'1' if hi(w0, 6)  else '0'}")
    print(f"    word[0] bit 7  : {'1' if hi(w0, 7)  else '0'}")
    print(f"    word[0] bit 8  : {'1' if hi(w0, 8)  else '0'}")
    print(f"    word[0] bit 9  : {'1' if hi(w0, 9)  else '0'}")
    print(f"    word[0] bit 10 : {'1' if hi(w0, 10) else '0'}")
    print(f"    word[0] bit 11 : {'1' if hi(w0, 11) else '0'}")
    print(f"    word[0] bit 12 : {'1' if hi(w0, 12) else '0'}")
    print(f"    word[0] bit 13 : {'1' if hi(w0, 13) else '0'}")
    print(f"    word[0] bit 14 : {'1' if hi(w0, 14) else '0'}")
    print(f"    word[0] bit 15 : {'1' if hi(w0, 15) else '0'}")
    print()
    print("  NOTE: Use 'rawwatch' mode and trigger each device to map bits to signals.")


def raw_dump(regs: list[int], label: str = "") -> None:
    """Print all 25 process I/O registers in hex."""
    prefix = f"[{label}] " if label else ""
    vals = "  ".join(f"r{i+PROC_ADDR}=0x{v:04X}" for i, v in enumerate(regs))
    print(f"{prefix}{vals}")


def raw_dump_changed(prev: list[int], curr: list[int]) -> None:
    """Print all registers, highlighting any that changed."""
    parts = []
    changed = False
    for i, (p, c) in enumerate(zip(prev, curr)):
        addr = PROC_ADDR + i
        if p != c:
            parts.append(f"r{addr}=0x{c:04X}(**was 0x{p:04X}**)")
            changed = True
        else:
            parts.append(f"r{addr}=0x{c:04X}")
    line = "  ".join(parts)
    marker = " <<< CHANGED" if changed else ""
    print(f"  {line}{marker}")


def decode_meta(regs: list[int]) -> None:
    """Decode the static device metadata block at address 2000."""
    print(f"  Device status metadata (address {META_ADDR}):")
    for i, v in enumerate(regs):
        print(f"    reg{META_ADDR+i} = 0x{v:04X}  ({v})")
    if regs:
        status = regs[0]
        status_map = {
            0x0000: "Pre-operational (boot)",
            0x0001: "Stopped",
            0x0002: "Initializing",
            0x0003: "Run (operational) -- EXPECTED",
            0x0004: "Error",
            0x0005: "Configuration mode",
        }
        meaning = status_map.get(status, f"Unknown status 0x{status:04X}")
        print(f"\n  Status code 0x{status:04X} = {meaning}")
        if status == 0x0003:
            print("  Device is running normally. Program loaded and active.")


if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print(f"Reading Sick Flexi live process I/O @ {FLEXI_IP} (addr {PROC_ADDR}) ...")
        c = plc()
        regs = read_process(c)
        c.close()
        decode(regs)

    elif cmd == "watch":
        print(f"Watching Sick Flexi live I/O @ {FLEXI_IP} (Ctrl+C to stop) ...")
        c = plc()
        try:
            while True:
                regs = read_process(c)
                w0, w1 = regs[0], regs[1]
                print(f"\r  word[0]=0x{w0:04X}  word[1]=0x{w1:04X}  ", end="", flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            c.close()

    elif cmd == "raw":
        print(f"Raw dump: all {PROC_COUNT} process I/O registers @ {FLEXI_IP} addr {PROC_ADDR} ...")
        c = plc()
        regs = read_process(c)
        c.close()
        raw_dump(regs)

    elif cmd == "rawwatch":
        HEARTBEAT_MASK = 0x8000  # bit 15 of reg256 toggles at 1Hz — suppress it
        print(f"Watching all {PROC_COUNT} registers @ {FLEXI_IP} addr {PROC_ADDR} (Ctrl+C to stop)...")
        print("Heartbeat (bit 15) suppressed. Trigger e-stop / light curtain / reset to map bits.")
        c = plc()
        try:
            prev = read_process(c)
            # mask heartbeat for comparison baseline
            prev_masked = list(prev)
            prev_masked[0] = prev[0] & ~HEARTBEAT_MASK
            print("  Baseline (bit 15 heartbeat masked):")
            raw_dump(prev_masked, "BASE")
            print()
            while True:
                time.sleep(0.25)
                curr = read_process(c)
                curr_masked = list(curr)
                curr_masked[0] = curr[0] & ~HEARTBEAT_MASK
                if curr_masked != prev_masked:
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[{ts}] CHANGE DETECTED:")
                    raw_dump_changed(prev_masked, curr_masked)
                    prev_masked = curr_masked
                else:
                    hb = "H" if (curr[0] & HEARTBEAT_MASK) else "L"
                    print(f"\r  no change  HB={hb}  r256=0x{curr_masked[0]:04X}  ", end="", flush=True)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            c.close()

    elif cmd == "meta":
        print(f"Reading device metadata @ {FLEXI_IP} addr {META_ADDR} ...")
        c = plc()
        regs = read_meta(c)
        c.close()
        decode_meta(regs)

    elif cmd == "reset_pstop":
        print(f"Pulsing P-Stop reset @ {FLEXI_IP} ...")
        print("NOTE: Write register address is TBC — ask David for confirmed output address.")
        c = plc()
        regs = read_process(c)
        print(f"  Current word[0] = 0x{regs[0]:04X}")
        print("  Writing 1 to output register ...")
        r = c.write_register(address=REG_OUT, value=1)
        if r.isError():
            print(f"  Write failed: {r}")
            print("  The output register address may be wrong. Ask David for OQ-23 answer.")
        else:
            time.sleep(0.2)
            print("  Writing 0 to output register ...")
            c.write_register(address=REG_OUT, value=0)
            time.sleep(0.5)
            regs = read_process(c)
            print(f"  Status after reset: word[0]=0x{regs[0]:04X}")
        c.close()

    else:
        print("Usage:")
        print("  python test_flexi.py status      -- decode live I/O once")
        print("  python test_flexi.py watch        -- live poll every 0.5s")
        print("  python test_flexi.py raw          -- dump all 25 live registers once")
        print("  python test_flexi.py rawwatch     -- live dump, highlights changes (use this!)")
        print("  python test_flexi.py meta         -- read static device metadata (addr 2000)")
        print("  python test_flexi.py reset_pstop  -- pulse P-Stop reset (addr TBC)")
