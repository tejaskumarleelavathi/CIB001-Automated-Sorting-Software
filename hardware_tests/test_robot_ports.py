import socket

ip = "172.16.9.15"
ports = [5007, 5008, 5009, 2000, 2001, 23, 80, 8080]

print(f"Scanning {ip} for open ports...")
for port in ports:
    try:
        s = socket.socket()
        s.settimeout(1)
        result = s.connect_ex((ip, port))
        if result == 0:
            print(f"  Port {port} OPEN ✅")
        else:
            print(f"  Port {port} closed")
        s.close()
    except Exception as e:
        print(f"  Port {port} error: {e}")
