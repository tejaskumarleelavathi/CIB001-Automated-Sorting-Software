from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("172.16.9.11", port=502)
client.connect()

result = client.write_register(address=1, value=1)

if result.isError():
    print(f"Write failed: {result}")
else:
    print("Written! Register 1 = 1")
    print("Watch the cell for any light or output turning on...")

client.close()
