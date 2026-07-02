"""
Real Sick Flexi Compact safety driver — ModbusTCP, read-only with one write.

WHAT IS CONFIRMED (2026-06-18 via test_flexi.py):
  - Device IP: 172.16.9.12, port 502
  - FC4 (Input Registers) at address 256: 25 words of live process I/O
  - FC4 at address 2000: device metadata — word 0 = 0x0003 means "Run" (expected)
  - Bit 15 of register 256 toggles at 1 Hz (heartbeat) — confirms device is live

WHAT IS PENDING (OQ-23 — David must answer before safety fault detection works):
  ┌───────────────────────────────────────────────────────────────────────────┐
  │ Run:  python hardware_tests/test_flexi.py rawwatch                        │
  │ Then physically trigger each device ONE AT A TIME:                        │
  │   1. Press e-stop button 0 → note which bits flip in reg 256              │
  │   2. Press e-stop button 1 (if any) → note bits                           │
  │   3. Break light curtain beam → note bits                                  │
  │   4. Press blue reset button → note bits                                   │
  │   5. Observe contactor state bit                                           │
  │ Fill in SAFETY_BIT_MAP below with the confirmed bit positions.             │
  │ Set BIT_MAP_CONFIRMED = True when done.                                   │
  └───────────────────────────────────────────────────────────────────────────┘

  Also needed from David:
  - Holding register address for writing protective-stop reset-acknowledge
    (releases light curtain latch when operator presses CONTINUE on HMI)

Until BIT_MAP_CONFIRMED = True, read_status() returns:
    any_fault_active = False  (optimistic — system assumed safe)
This means the safety poll loop runs without triggering false faults, but
actual light curtain / e-stop detection is disabled. Do the rawwatch session
and fill in the map before going to production.

Device:  172.16.9.12, port 502, ModbusTCP, FC4 for reads
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from pymodbus.client import ModbusTcpClient

log = structlog.get_logger(__name__)

# ── Hardware constants ────────────────────────────────────────────────────────

FLEXI_IP   = "172.16.9.12"
FLEXI_PORT = 502

# Process I/O block — live signal state (FC4, Input Registers)
PROC_ADDR  = 256
PROC_COUNT = 25

# Device metadata block (static — confirms Run mode)
META_ADDR  = 2000

# Heartbeat: bit 15 of register 256 toggles at 1 Hz when device is alive.
HEARTBEAT_BIT = 15
HEARTBEAT_MASK = 1 << HEARTBEAT_BIT   # 0x8000

# ── BIT MAP (OQ-23 — fill in after rawwatch session) ─────────────────────────
#
# Each entry: signal_name → (reg_index, bit_number)
#   reg_index  = index into the 25-register process I/O block (0 = reg 256)
#   bit_number = 0 (LSB) … 15 (MSB) within that 16-bit register
#
# Convention: bit = 1 means the signal is ACTIVE / TRIPPED / CLOSED.
# Verify this convention during the rawwatch session — some Flexi outputs
# are normally-open (0=OK, 1=fault) and some are normally-closed (1=OK, 0=fault).
# Add a note in the comment if convention is inverted for a particular signal.
#
SAFETY_BIT_MAP: dict[str, tuple[int, int]] = {
    # "light_curtain":  (0, 0),   # reg256 bit 0 — fill in from rawwatch
    # "estop_button_0": (0, 1),   # reg256 bit 1
    # "estop_button_1": (0, 2),   # reg256 bit 2 (if second e-stop button exists)
    # "contactor":      (0, 3),   # reg256 bit 3
    # "reset_button":   (0, 4),   # reg256 bit 4
}

# Set True once every entry above is confirmed by the rawwatch session.
# When False, read_status() returns safe defaults (no faults).
BIT_MAP_CONFIRMED = False

# Holding register address for writing the protective-stop reset-acknowledge.
# Pulsing this releases the light curtain latch when operator presses CONTINUE.
# OQ-23 PENDING — ask David for the confirmed address.
RESET_ACK_REG: int | None = None   # e.g. 256 or whatever David confirms


# ── Driver ────────────────────────────────────────────────────────────────────

class FlexiCompactDriver:
    """
    Sick Flexi Compact safety driver.

    Polls process I/O at address 256 every 100ms (called by orchestrator).
    Decodes fault bits when BIT_MAP_CONFIRMED = True.
    Until then, returns optimistic defaults so the orchestrator doesn't fault.
    """

    def __init__(
        self,
        ip: str = FLEXI_IP,
        port: int = FLEXI_PORT,
        connect_timeout_s: float = 3.0,
    ) -> None:
        self._ip      = ip
        self._port    = port
        self._timeout = connect_timeout_s
        self._client: ModbusTcpClient | None = None
        self._last_heartbeat_bit: int | None = None

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        client = ModbusTcpClient(self._ip, port=self._port, timeout=self._timeout)
        if not client.connect():
            raise RuntimeError(
                f"Sick Flexi at {self._ip}:{self._port} refused connection. "
                "Check Ethernet cable and Flexi power / Run mode."
            )
        # Verify device is in Run mode (metadata reg 0 = 0x0003)
        r = client.read_input_registers(address=META_ADDR, count=1)
        if r.isError():
            client.close()
            raise RuntimeError(f"Flexi metadata read failed: {r}")
        status_word = r.registers[0]
        if status_word != 0x0003:
            log.warning(
                "flexi.unexpected_status",
                status=f"0x{status_word:04X}",
                expected="0x0003 (Run)",
                note="device may be in config or error mode",
            )
        self._client = client
        log.info(
            "flexi.connected",
            ip=self._ip,
            port=self._port,
            device_status=f"0x{status_word:04X}",
            bit_map_confirmed=BIT_MAP_CONFIRMED,
        )
        if not BIT_MAP_CONFIRMED:
            log.warning(
                "flexi.bit_map_pending",
                reason="OQ-23 open — SAFETY_BIT_MAP not confirmed yet",
                action="run rawwatch session + fill in SAFETY_BIT_MAP in safety/driver.py",
                impact="e-stop / light-curtain detection DISABLED until confirmed",
            )

    async def disconnect(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        log.info("flexi.disconnected")

    # ------------------------------------------------------------------
    # read_status — called every 100ms by orchestrator safety poll loop
    # ------------------------------------------------------------------

    async def read_status(self) -> dict[str, Any]:
        """
        Returns:
            any_fault_active:       bool — True if any fault detected
            light_curtain_tripped:  bool
            estop_active:           bool
            estop_button_index:     int | None
            contactor_closed:       bool
            reset_available:        bool
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_status_sync)

    def _read_status_sync(self) -> dict[str, Any]:
        c = self._client
        if c is None:
            raise RuntimeError("Flexi not connected")

        r = c.read_input_registers(address=PROC_ADDR, count=PROC_COUNT)
        if r.isError():
            raise RuntimeError(f"Flexi process I/O read failed: {r}")

        regs = r.registers

        # ── Heartbeat check ───────────────────────────────────────────────────
        # Bit 15 of reg 256 toggles at 1 Hz. If it's stuck for several polls
        # the device may have stopped communicating even though TCP is still up.
        # We track it here for logging; the orchestrator handles lost connection
        # via the ConnectionLostEvent path if read_input_registers raises.
        hb = (regs[0] >> HEARTBEAT_BIT) & 1
        if self._last_heartbeat_bit is not None and hb == self._last_heartbeat_bit:
            # Heartbeat didn't toggle — could be aliasing at 100ms poll vs 1Hz,
            # or a stuck device. Log at debug level; don't fault on a single miss.
            log.debug("flexi.heartbeat_same", bit=hb,
                      note="expected to toggle ~1Hz; may be aliasing at 100ms poll")
        self._last_heartbeat_bit = hb

        # ── Fault decode ──────────────────────────────────────────────────────
        if not BIT_MAP_CONFIRMED:
            # OQ-23 not answered yet — return optimistic defaults.
            # The raw register values are logged at debug so the rawwatch
            # session output can be compared if needed.
            log.debug("flexi.raw_io",
                      r256=f"0x{regs[0]:04X}",
                      r257=f"0x{regs[1]:04X}" if len(regs) > 1 else "n/a")
            return {
                "any_fault_active":      False,
                "light_curtain_tripped": False,
                "estop_active":          False,
                "estop_button_index":    None,
                "contactor_closed":      True,
                "reset_available":       False,
            }

        # ── Confirmed bit map decode ──────────────────────────────────────────
        def _bit(signal: str) -> bool:
            if signal not in SAFETY_BIT_MAP:
                return False
            reg_idx, bit_num = SAFETY_BIT_MAP[signal]
            if reg_idx >= len(regs):
                return False
            return bool((regs[reg_idx] >> bit_num) & 1)

        light_curtain = _bit("light_curtain")
        estop_0       = _bit("estop_button_0")
        estop_1       = _bit("estop_button_1")
        contactor     = _bit("contactor")
        reset_btn     = _bit("reset_button")

        estop_active = estop_0 or estop_1
        estop_idx: int | None = None
        if estop_0:
            estop_idx = 0
        elif estop_1:
            estop_idx = 1

        any_fault = estop_active or light_curtain or not contactor

        return {
            "any_fault_active":      any_fault,
            "light_curtain_tripped": light_curtain,
            "estop_active":          estop_active,
            "estop_button_index":    estop_idx,
            "contactor_closed":      contactor,
            "reset_available":       reset_btn,
        }

    # ------------------------------------------------------------------
    # Protective-stop reset acknowledge (OQ-23 PENDING)
    # ------------------------------------------------------------------

    async def send_reset_acknowledge(self) -> None:
        """
        Pulse the protective-stop reset-acknowledge register to release the
        light curtain latch. Only call this when:
          (a) light curtain is physically clear (confirmed by read_status), AND
          (b) operator has pressed CONTINUE on the HMI.

        OQ-23 PENDING: RESET_ACK_REG is None until David confirms the address.
        """
        if RESET_ACK_REG is None:
            log.warning(
                "flexi.reset_ack.not_implemented",
                reason="OQ-23 open — RESET_ACK_REG address not confirmed by David",
            )
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._pulse_reset_sync)

    def _pulse_reset_sync(self) -> None:
        c = self._client
        if c is None:
            raise RuntimeError("Flexi not connected")
        r = c.write_register(address=RESET_ACK_REG, value=1)
        if r.isError():
            raise RuntimeError(f"Flexi reset-ack write failed: {r}")
        import time
        time.sleep(0.1)
        r = c.write_register(address=RESET_ACK_REG, value=0)
        if r.isError():
            log.warning("flexi.reset_ack.clear_failed", error=str(r))
        log.info("flexi.reset_ack_sent", register=RESET_ACK_REG)
