"""
Real Epson RC+ 7.0 "Remote Ethernet" driver — controller-level lifecycle
control over a raw TCP socket, built into the controller firmware (Control
Device = Remote Ethernet, no separate license required — distinct from the
paid "RC+ API" .NET option).

Protocol (EPSON RC+ 7.0 (Ver.7.5) User's Guide, section 12.2):
    Python sends:  $Command{,param1,param2,...}<CR><LF>
    Robot replies: #Command,[value]<CR><LF>          on success
                or: !Command,[ErrorCode]<CR><LF>      on failure

Commands used here (full list in the User's Guide has many more — I/O and
variable access, backup parameters, arbitrary SPEL+ Execute, etc. — this
driver only wraps the supervisory subset the orchestrator actually needs):
    Login,<password>       -> must be sent first; nothing else works until
                               this succeeds
    Logout                 -> ends the Remote Ethernet session
    GetStatus               -> #GetStatus,<11-char flag string>,<4-digit err code>
                               Flags in order: Test/Teach/Auto/Warning/SError/
                               Safeguard/EStop/Error/Paused/Running/Ready
    Reset,<robot#>          -> clears E-stop/error (0 = all manipulators)
    SetMotorsOn,<robot#>    -> motor on
    SetMotorsOff,<robot#>   -> motor off
    Start,<function#>       -> starts Main[function#] (0 = Main, 1 = Main1, ...)
    Stop,<robot#>           -> stops all tasks and commands
    Home                    -> moves to the user-defined home position

The controller must have Control Device = Remote Ethernet configured in RC+
(System Configuration -> Controller -> Configuration) and a port + password
set on the System Configuration -> Remote Control -> Ethernet page — both
one-time setup steps done in RC+, not from here. IP/port/password below are
placeholders until that configuration exists — see machine_config.yaml.

Client equipment must wait for GetStatus's Auto flag to be ON before any
command other than Login/Logout/Get* is accepted — see wait_until_ready().
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ROBOT_REMOTE_IP   = "172.16.9.15"   # same controller as cib.drivers.robot — different port
ROBOT_REMOTE_PORT = 5000            # PLACEHOLDER — set to whatever port is configured on the
                                     # System Configuration -> Remote Control -> Ethernet page

CONNECT_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 10.0

# Flag order in the GetStatus response string — fixed by the protocol, not
# our choice. Index 0 = leftmost character.
_STATUS_FLAG_NAMES = (
    "test", "teach", "auto", "warning", "serror", "safeguard",
    "estop", "error", "paused", "running", "ready",
)

# Error codes from the User's Guide, section 12.2.7 "Response" -> "Error response"
_ERROR_MEANINGS = {
    "10": "Remote command does not begin with $",
    "11": "Remote command is wrong, or Login was not executed",
    "12": "Remote command format is wrong",
    "13": "Login command password is wrong",
    "14": "Parameter/acquire-count problem (out of range, omitted, or wrong type)",
    "15": "Parameter does not exist, wrong dimension, or element out of range",
    "19": "Request timed out",
    "20": "Controller is not ready",
    "21": "Cannot execute — an Execute command is already running",
    "98": "Password is required for Login when using a global IP address",
    "99": "System error / communication error",
}


@dataclass
class RemoteStatus:
    """Parsed GetStatus response. See _STATUS_FLAG_NAMES for flag order."""
    test:      bool
    teach:     bool
    auto:      bool
    warning:   bool
    serror:    bool
    safeguard: bool
    estop:     bool
    error:     bool
    paused:    bool
    running:   bool
    ready:     bool
    error_code: str   # "0000" = no error/warning


class RemoteEthernetError(RuntimeError):
    """Raised on a !Command,ErrorCode response from the controller."""

    def __init__(self, command: str, error_code: str) -> None:
        meaning = _ERROR_MEANINGS.get(error_code, "Unknown error code")
        super().__init__(f"{command} failed: error {error_code} ({meaning})")
        self.command = command
        self.error_code = error_code


class RemoteEthernetDriver:
    """
    Epson RC+ Remote Ethernet driver — controller lifecycle control.

    connect() opens the socket and logs in. Every other method requires a
    successful connect() first. wait_until_ready() must be called (and
    return True) before any command other than get_status()/reset() is
    guaranteed to be accepted — see the module docstring.
    """

    def __init__(
        self,
        ip: str = ROBOT_REMOTE_IP,
        port: int = ROBOT_REMOTE_PORT,
        password: str = "",
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        command_timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> None:
        self._ip = ip
        self._port = port
        self._password = password
        self._connect_timeout = connect_timeout_s
        self._command_timeout = command_timeout_s
        self._sock: socket.socket | None = None
        # Serialises send/recv exchanges — same rationale as cib.drivers.robot:
        # this driver's poll loop and any on-demand recovery call must never
        # interleave their reads/writes on the same TCP stream.
        self._cmd_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        log.info("robot_remote.connecting", ip=self._ip, port=self._port)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self._connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            s.connect((self._ip, self._port))
        except OSError as exc:
            raise RuntimeError(
                f"Cannot connect to robot Remote Ethernet at {self._ip}:{self._port} — {exc}\n"
                "Check: Control Device is set to Remote Ethernet in RC+ System "
                "Configuration, and the port here matches the Remote Control -> "
                "Ethernet page."
            ) from exc
        self._sock = s

        with self._cmd_lock:
            self._call("Login", [self._password])
        log.info("robot_remote.connected", ip=self._ip, port=self._port)

    async def disconnect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        if self._sock is None:
            return
        with self._cmd_lock:
            try:
                self._call("Logout", [])
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        log.info("robot_remote.disconnected")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> RemoteStatus:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_status_sync)

    def _get_status_sync(self) -> RemoteStatus:
        with self._cmd_lock:
            value = self._call("GetStatus", [])
        # value is "[11-char flag string],[4-digit error code]"
        parts = value.split(",")
        if len(parts) != 2 or len(parts[0]) != len(_STATUS_FLAG_NAMES):
            raise RuntimeError(f"GetStatus returned unexpected format: {value!r}")
        flags_str, error_code = parts
        flags = {name: (flags_str[i] == "1") for i, name in enumerate(_STATUS_FLAG_NAMES)}
        return RemoteStatus(**flags, error_code=error_code)

    async def wait_until_ready(self, timeout_s: float = 30.0, poll_interval_s: float = 0.2) -> bool:
        """
        Poll GetStatus until the Auto flag is ON — required by the protocol
        before any control command (Reset, SetMotorsOn, Start, ...) is
        guaranteed to be accepted. Returns False on timeout rather than
        raising, so the caller can decide how to react.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = await self.get_status()
            if status.auto:
                return True
            await asyncio.sleep(poll_interval_s)
        log.warning("robot_remote.wait_until_ready_timeout", timeout_s=timeout_s)
        return False

    # ------------------------------------------------------------------
    # Control commands
    # ------------------------------------------------------------------

    async def reset(self, robot_number: int = 0) -> None:
        """Clear E-stop/error. 0 = all manipulators. Does NOT re-enable motors."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "Reset", [robot_number])
        log.info("robot_remote.reset", robot_number=robot_number)

    async def set_motors_on(self, robot_number: int = 0) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "SetMotorsOn", [robot_number])
        log.info("robot_remote.motors_on", robot_number=robot_number)

    async def set_motors_off(self, robot_number: int = 0) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "SetMotorsOff", [robot_number])
        log.info("robot_remote.motors_off", robot_number=robot_number)

    async def start(self, function_no: int = 0) -> None:
        """Start Main[function_no] — 0 = Main, 1 = Main1, ... 63 = Main63.
        This is what launches sort_server.prg's task; replaces manually
        clicking Run in the RC+ development environment."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "Start", [function_no])
        log.info("robot_remote.start", function_no=function_no)

    async def stop(self, robot_number: int = 0) -> None:
        """Stop all tasks and commands."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "Stop", [robot_number])
        log.info("robot_remote.stop", robot_number=robot_number)

    async def home(self) -> None:
        """Move to the user-defined home position (P0)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._call_locked, "Home", [])
        log.info("robot_remote.home")

    def _call_locked(self, command: str, params: list) -> str:
        with self._cmd_lock:
            return self._call(command, params)

    # ------------------------------------------------------------------
    # TCP helpers
    # ------------------------------------------------------------------

    def _call(self, command: str, params: list) -> str:
        """
        Send one $Command,params request, return the value portion of a
        successful #Command,value response. Raises RemoteEthernetError on
        a !Command,ErrorCode response. Caller must hold _cmd_lock.
        """
        if self._sock is None:
            raise ConnectionError("Robot Remote Ethernet not connected — socket closed")

        parts = [command] + [str(p) for p in params]
        line = "$" + ",".join(parts)
        self._sock.settimeout(self._command_timeout)
        try:
            self._sock.sendall((line + "\r\n").encode("ascii"))
            resp = self._recv_line()
        except socket.timeout as exc:
            raise RuntimeError(
                f"Robot Remote Ethernet timed out after {self._command_timeout}s "
                f"waiting for response to: {line!r}"
            ) from exc

        if resp.startswith("#"):
            # "#Command,value" or "#Command,value1,value2,..." (GetStatus, GetAlm, etc.)
            _, _, value = resp.partition(",")
            return value
        if resp.startswith("!"):
            body = resp[1:]
            resp_command, _, error_code = body.partition(",")
            raise RemoteEthernetError(resp_command, error_code.strip())
        raise RuntimeError(f"Unexpected Remote Ethernet response: {resp!r}")

    def _recv_line(self) -> str:
        buf = b""
        while True:
            chunk = self._sock.recv(1)
            if not chunk:
                raise ConnectionError("Robot Remote Ethernet closed the connection unexpectedly")
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return buf.decode("ascii", errors="replace").strip()
