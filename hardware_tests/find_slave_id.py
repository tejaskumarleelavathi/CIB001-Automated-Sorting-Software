from pymodbus.client import ModbusTcpClient

c = ModbusTcpClient("172.16.9.11", port=502)
c.connect()

print("Testing slave IDs...")
for slave_id in [0, 1, 2, 255]:
    try:
        r = c.read_coils(address=0, count=1, no_response_expected=False)
        if not r.isError():
            print(f"  Slave ID {slave_id} → responded! coil 0 = {r.bits[0]}")
        else:
            print(f"  Slave ID {slave_id} → error: {r}")
    except Exception as e:
        print(f"  Slave ID {slave_id} → exception: {e}")

c.close()
