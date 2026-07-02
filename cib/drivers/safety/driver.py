"""
Real Sick Flexi Compact safety driver — ModbusTCP, read-only with one write.

CONFIRMED BIT MAP (2026-07-02, rawwatch session):
  Baseline (all safe): reg256 = 0x0517  (bits 0,1,2,4,8,10 = 1)

  Convention: normally-closed — bit = 1 means OK/safe, 0 means fault/open.

  bit 0  — E-stop button OK       (1=not pressed, 0=button is pressed)
  bit 1  — Unknown input           (always 1 in tests — spare or door interlock)
  bit 2  — Light curtain OK        (1=beam intact/safe, 0=beam broken/tripped)
  bit 3  — Reset button active     (1=being pressed, 0=not pressed)
  bit 4  — Unknown input           (always 1 in tests — spare or door interlock)
  bit 8  — Safety contactor closed (1=closed/running, 0=open/tripped by e-stop)
  bit 9  — Reset available         (1=e-stop released but reset not yet pressed)
  bit 10 — Safety output OK        (1=all safe, 0=any active fault — latches until reset)
  bit 11 — Transient reset state   (appears briefly during reset sequence)
  bit 15 — Heartbeat               (toggles at 1Hz — suppressed by rawwatch, masked in read)

  State transitions observed:
    Normal:               0x0517  bits 0,1,2,4,8,10 = 1
    E-stop pressed:       0x0016  bits 8,10 lost; bit 0 lost (button down)
    E-stop released:      0x0217  bit 0 restored; bit 9 set (awaiting reset)
    Reset button pressed: 0x001F  bit 3 set; bit 9 cleared
    Contactor re-closes:  0x0917  bit 8 restored (reset sequence progressing)
    Back to normal:       0x0517  bit 10 restored
    Light curtain broken: 0x0113  bits 2 and 10 lost; bit 8 stays (protective stop)
    Light curtain clear:  0x0917  bit 2 restored; bit 10 still 0 (latched until HMI CONTINUE)

RESET ACKNOWLEDGE WRITE:
  RESET_ACK_REG = None — address not yet confirmed.
  The Flexi holds the light curtain latch even after the beam clears.
  Releasing it requires writing to a Flexi output/holding register.
  Ask David for the Modbus write address (OQ-23 remaining item).
  Until confirmed, send_reset_acknowledge() is a no-op.

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

PROC_ADDR  = 256    # Input registers — live process I/O (FC4)
PROC_COUNT = 25
META_ADDR  = 2000   # Static device metadata

HEARTBEAT_MASK = 0x8000   # bit 15 — toggling 1Hz, masked out before decode

# ── Confirmed bit positions (reg 256, 0-based bit index) ─────────────────────
# All bits use normally-closed convention: 1=OK/safe, 0=fault/open

_BIT_ESTOP_BUTTON   = 0    # e-stop button: 0 while pressed, 1 when released
_BIT_LIGHT_CURTAIN  = 2    # light curtain: 0 when beam broken, 1 when clear
_BIT_RESET_BUTTON   = 3    # reset button: 1 while being pressed
_BIT_CONTACTOR      = 8    # safety contactor: 0 when open (e-stop fired)
_BIT_RESET_AVAIL    = 9    # 1 when e-stop released but reset not yet pressed
_BIT_SAFETY_OK      = 10   # overall safety output: 0 = any fault latched

# ── Reset acknowledge write register (OQ-23 partially resolved) ───────────────
# Set to the Modbus holding register address David confirms for the light
# curtain latch release signal. None until confirmed.
RESET_ACK_REG: int | None = None


class FlexiCompactDriver:
    """
    Sick Flexi Compact safety driver.
    Reads process I/O at address 256 every 100ms (orchestrator safety poll loop).
    Bit map confirmed 2026-07-02 via rawwatch session.
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
        self._last_hb: int | None = None

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
            )
        self._client = client
        log.info("flexi.connected", ip=self._ip, port=self._port,
                 device_status=f"0x{status_word:04X}")
        if RESET_ACK_REG is None:
            log.warning(
                "flexi.reset_ack_pending",
                note="Light curtain latch release write address not confirmed (OQ-23). "
                     "send_reset_acknowledge() is a no-op until RESET_ACK_REG is set.",
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
    # read_status — called every 100ms by orchestrator
    # ------------------------------------------------------------------

    async def read_status(self) -> dict[str, Any]:
        """
        Returns:
            any_fault_active:       bool
            light_curtain_tripped:  bool  — beam broken (protective stop)
            estop_active:           bool  — e-stop condition latched (contactor open)
            estop_button_index:     int | None  — 0 = only button confirmed
            contactor_closed:       bool
            reset_available:        bool  — e-stop released, awaiting reset button
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
        w = regs[0] & ~HEARTBEAT_MASK   # mask heartbeat bit before decode

        def bit(n: int) -> bool:
            return bool((w >> n) & 1)

        # Normally-closed: bit=1 means OK, so we INVERT for "tripped/active"
        estop_btn_pressed  = not bit(_BIT_ESTOP_BUTTON)   # True when button down
        light_curtain_ok   = bit(_BIT_LIGHT_CURTAIN)
        contactor_closed   = bit(_BIT_CONTACTOR)
        reset_available    = bit(_BIT_RESET_AVAIL)
        safety_ok          = bit(_BIT_SAFETY_OK)

        # Light curtain tripped = beam broken RIGHT NOW (bit 2 low)
        light_curtain_tripped = not light_curtain_ok

        # E-stop "active" = contactor is open AND light curtain is fine
        # Covers: button currently pressed, AND latched state after release
        estop_active = (not contactor_closed) and light_curtain_ok

        # Overall fault = safety output latched low (any fault, any state)
        any_fault_active = not safety_ok

        estop_button_index: int | None = None
        if estop_active or estop_btn_pressed:
            estop_button_index = 0   # one e-stop button confirmed

        log.debug(
            "flexi.status",
            raw=f"0x{w:04X}",
            any_fault=any_fault_active,
            estop=estop_active,
            light_curtain=light_curtain_tripped,
            contactor=contactor_closed,
            reset_avail=reset_available,
        )

        return {
            "any_fault_active":      any_fault_active,
            "light_curtain_tripped": light_curtain_tripped,
            "estop_active":          estop_active,
            "estop_button_index":    estop_button_index,
            "contactor_closed":      contactor_closed,
            "reset_available":       reset_available,
        }

    # ------------------------------------------------------------------
    # Protective-stop reset acknowledge
    # ------------------------------------------------------------------

    async def send_reset_acknowledge(self) -> None:
        """
        Pulse the protective-stop reset-acknowledge register to release the
        light curtain latch. Only called when light curtain is physically
        clear AND operator has pressed CONTINUE on the HMI.

        RESET_ACK_REG is None until David confirms the write address (OQ-23).
        """
        if RESET_ACK_REG is None:
            log.warning(
                "flexi.reset_ack.no_op",
                reason="RESET_ACK_REG not set — ask David for OQ-23 write address",
            )
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._pulse_reset_sync)

    def _pulse_reset_sync(self) -> None:
        import time
        c = self._client
        if c is None:
            raise RuntimeError("Flexi not connected")
        r = c.write_register(address=RESET_ACK_REG, value=1)
        if r.isError():
            raise RuntimeError(f"Flexi reset-ack write failed: {r}")
        time.sleep(0.1)
        c.write_register(address=RESET_ACK_REG, value=0)
        log.info("flexi.reset_ack_sent", register=RESET_ACK_REG)
