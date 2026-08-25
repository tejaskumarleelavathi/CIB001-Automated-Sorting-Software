"""
watch_recovery.py -- just polls port 2000 (real STATUS/OK READY check) every
2s for up to 100s, without touching anything else. Use this after a
connection has already been silently killed, to see if/when sort_server.prg
recovers on its own.

Usage:
    conda run -n cib_env python hardware_tests/watch_recovery.py
"""

import socket
import sys
import time

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config


def probe(ip: str, timeout: float = 1.5) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, 2000))
        s.sendall(b"STATUS\r\n")
        resp = s.recv(1024)
        if b"OK READY" in resp:
            return "OK"
        return f"unexpected: {resp!r}"
    except Exception as exc:
        return f"refused ({exc.__class__.__name__})"
    finally:
        s.close()


def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    ip = cfg.robot_remote.ip
    t0 = time.monotonic()
    for _ in range(50):
        elapsed = time.monotonic() - t0
        state = probe(ip)
        print(f"t={elapsed:6.1f}s  {state}")
        if state == "OK":
            print("RECOVERED")
            return
        time.sleep(2.0)
    print("STILL NOT RECOVERED after 100s")


if __name__ == "__main__":
    main()
