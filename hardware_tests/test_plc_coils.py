from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("172.16.9.11", port=502)
client.connect()

result = client.write_coil(address=0, value=True)
if result.isError():
    print(f"Failed: {result}")
else:
    rb = client.read_coils(address=0, count=1)
    print(f"Coil 0 = {rb.bits[0]}")

client.close()
