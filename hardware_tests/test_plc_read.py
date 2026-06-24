"""
PLC Register Read Test
AutomationDirect P1-622 @ 172.16.9.11

Reads holding registers, coils, and input registers across a wide range
so we can see what the PLC has configured and figure out the register map.
Safe — read only, nothing is written.
"""

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

PLC_IP   = "172.16.9.11"
PLC_PORT = 502


def read_block(client, label, read_fn, start, count):
    """Read a block of registers and print results."""
    print(f"\n--- {label} (address {start} to {start + count - 1}) ---")
    try:
        result = read_fn(address=start, count=count)
        if result.isError():
            print(f"  Error: {result}")
        else:
            values = result.registers if hasattr(result, "registers") else result.bits
            for i, val in enumerate(values):
                if val != 0:  # highlight non-zero values
                    print(f"  [{start + i:>4}] = {val}  *** NON-ZERO ***")
                else:
                    print(f"  [{start + i:>4}] = {val}")
    except ModbusException as e:
        print(f"  Modbus exception: {e}")
    except AttributeError as e:
        print(f"  Could not read: {e}")


def main():
    print(f"Connecting to PLC at {PLC_IP}:{PLC_PORT} ...")
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT)

    if not client.connect():
        print("ERROR: Could not connect to PLC. Check IP and that PLC is powered on.")
        return

    print("Connected!\n")

    # --- Holding Registers (most common for AutomationDirect) ---
    # These are read/write registers — likely where outputs are controlled
    read_block(client, "Holding Registers 0-49",
               client.read_holding_registers, 0, 50)

    read_block(client, "Holding Registers 50-99",
               client.read_holding_registers, 50, 50)

    read_block(client, "Holding Registers 100-149",
               client.read_holding_registers, 100, 50)

    # --- Coils (single bit on/off outputs) ---
    # On AutomationDirect PLCs, discrete outputs are often coils
    print("\n--- Coils 0-49 (discrete on/off outputs) ---")
    try:
        result = client.read_coils(address=0, count=50)
        if result.isError():
            print(f"  Error: {result}")
        else:
            for i, val in enumerate(result.bits[:50]):
                if val:
                    print(f"  [coil {i:>3}] = ON  *** ON ***")
                else:
                    print(f"  [coil {i:>3}] = off")
    except Exception as e:
        print(f"  Could not read coils: {e}")

    # --- Discrete Inputs (read-only physical inputs) ---
    print("\n--- Discrete Inputs 0-49 (physical input signals) ---")
    try:
        result = client.read_discrete_inputs(address=0, count=50)
        if result.isError():
            print(f"  Error: {result}")
        else:
            for i, val in enumerate(result.bits[:50]):
                if val:
                    print(f"  [input {i:>3}] = ON  *** ON ***")
                else:
                    print(f"  [input {i:>3}] = off")
    except Exception as e:
        print(f"  Could not read discrete inputs: {e}")

    # --- Input Registers (read-only analog/status values) ---
    read_block(client, "Input Registers 0-49",
               client.read_input_registers, 0, 50)

    client.close()
    print("\nDone. Disconnected from PLC.")
    print("\nLook for *** NON-ZERO *** or *** ON *** lines above.")
    print("Those are registers that have values — likely the active ones.")


if __name__ == "__main__":
    main()
