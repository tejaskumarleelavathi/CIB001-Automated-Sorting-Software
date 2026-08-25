"""
stress_dropout_fix.py -- repeat the silent-connect-then-close failure mode
10x back to back against the already-running sort_server.prg, checking
recovery after each one. Does NOT call start()/reset()/motors -- assumes
the program is already running (run verify_dropout_fix.py first).

Usage:
    conda run -n cib_env python hardware_tests/stress_dropout_fix.py
"""

import socket
import sys
import time

sys.path.insert(0, ".")

from cib.config.loader import load_machine_config


def probe_port_2000(ip: str, timeout: float = 1.0) -> str:
    """True functional check -- a bare TCP connect() can succeed even when
    the server app hasn't actually accepted the client (OS backlog queue),
    so require a real STATUS/OK READY round-trip."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, 2000))
        s.sendall(b"STATUS\r\n")
        resp = s.recv(1024)
        if b"OK READY" in resp:
            return "OK"
        return f"unexpected response: {resp!r}"
    except Exception as exc:
        return f"refused ({exc.__class__.__name__})"
    finally:
        s.close()


def kill_connection_silently(ip: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((ip, 2000))
    except Exception:
        pass
    finally:
        s.close()


def main() -> None:
    cfg = load_machine_config("config/machine_config.yaml")
    ip = cfg.robot_remote.ip

    N_ROUNDS = 3
    WINDOW_S = 75  # port 201's SetNet Timeout is now 60s -- give real margin

    results = []
    for i in range(N_ROUNDS):
        kill_connection_silently(ip)
        t0 = time.monotonic()
        recovered_at = None
        for _ in range(WINDOW_S):
            state = probe_port_2000(ip)
            if state == "OK":
                recovered_at = time.monotonic() - t0
                break
            time.sleep(1.0)
        results.append(recovered_at)
        print(f"round {i+1:2}: {'recovered in ' + f'{recovered_at:.2f}s' if recovered_at is not None else f'FAILED TO RECOVER within {WINDOW_S}s'}")

    n_ok = sum(1 for r in results if r is not None)
    print(f"\n{n_ok}/{N_ROUNDS} rounds recovered.")
    if n_ok > 0:
        print(f"recovery times: {[f'{r:.2f}' for r in results if r is not None]}")


if __name__ == "__main__":
    main()
