from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("172.16.9.11", port=502)
client.connect()

# clean up — write 0 back
client.write_register(address=1, value=0)
print("Cleaned up — Register 1 reset to 0")

# confirm
result = client.read_holding_registers(address=1, count=1)
print(f"Register 1 now = {result.registers[0]}")

client.close()
