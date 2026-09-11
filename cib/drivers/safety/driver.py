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

RESET ACKNOWLEDGE WRITE (OQ-23 RESOLVED 2026-09-11):
  Holding register 256, BIT 8 (write value 256, then 0) — confirmed live
  on real hardware: with a light-curtain trip latched (raw=0x0917,
  safety_ok=0), pulsing this flipped safety_ok 0->1 and brought raw back
  to the documented fully-normal baseline (0x0517). Found by scanning
  every bit position (not just bit 0, which every earlier attempt tried
  and which does nothing) — see RESET_ACK_REG's own comment below for why
  bit 8 specifically (a byte-swap in this device's Modbus gateway).
  See send_reset_acknowledge()/_pulse_reset_sync() below.

Device:  172.16.9.12, port 502, ModbusTCP, FC4 for reads
"""

from __future__ import annotations

import asyncio
import threading
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
_BIT_EDM_OKAY       = 4    # external device monitoring feedback OK
_BIT_CONTACTOR      = 8    # safety contactor: 0 when open (e-stop fired) --
                            # cross-validated 2026-09-11 against the Safety
                            # Designer offline report's own logic diagram:
                            # this is actually "Enable System", the E-STOP
                            # side's own Reset V1 block's output. P-Stop's
                            # Reset V1 (what send_reset_acknowledge() below
                            # targets) can only ever release once THIS is
                            # already 1 -- if a light-curtain-only recovery
                            # keeps failing no matter how long the curtain
                            # stays clear, check this bit specifically; it
                            # may mean an e-stop button + blue reset cycle
                            # has never actually happened this session.
_BIT_RESET_AVAIL    = 9    # 1 when e-stop released but reset not yet pressed
_BIT_SAFETY_OK      = 10   # overall safety output: 0 = any fault latched --
                            # cross-validated 2026-09-11: literally "P. Stop
                            # Enabled" (does the motor have power), not a
                            # generic any-fault bit -- functionally correct
                            # for gating either way since e-stop cuts the
                            # same motor power, just worth knowing precisely
                            # what it means when diagnosing.
_BIT_P_STOP_RESET_READY = 11   # Safety Designer report's "P. Stop Reset
                            # Required" -- best current read: P-Stop's
                            # Reset V1 block is willing to accept an
                            # attempt, not necessarily that Release (Enable
                            # System AND curtain clear) is true right now.

# ── Reset acknowledge write register (OQ-23 RESOLVED 2026-09-11) ──────────────
# Confirmed live on real hardware: holding register 256, BIT 8 (write value
# 256, i.e. 1<<8 -- not bit 0 / value 1, which is what every earlier attempt
# tried and which does nothing). Verified by a full 16-bit-position pulse
# scan of register 256 while a real light-curtain trip was latched: writing
# 256 then 0 flipped safety_ok 0->1 and brought raw back to the documented
# fully-normal baseline (0x0517) -- reproduced cleanly, not a fluke.
#
# Why bit 8: Safety Designer's own offline report names this signal "Reset
# Motor" -- byte 0, bit 0 of the "Network to CPU" gateway data object. But
# comparing that report's read-side ("CPU to Network") byte/bit table
# against this driver's own field-verified bit map (built 2026-07-02 by
# watching register 256 while physically pressing buttons) shows this
# device BYTE-SWAPS: "Byte 1" occupies the low 8 bits of its register and
# "Byte 0" occupies the high 8 bits, not the naive same-order mapping. The
# write side apparently follows the same convention, putting bit 0 of its
# one byte at absolute bit 8 of holding register 256.
#
# This register number happens to equal the read-side INPUT register
# address (also 256) -- a coincidence of this device's own design
# (Modbus input registers and holding registers are genuinely separate
# address spaces; nothing about the protocol requires them to share a
# number), not something derivable from the Modbus spec itself.
RESET_ACK_REG: int | None = 256
RESET_ACK_BIT: int = 8   # absolute bit position within RESET_ACK_REG
RESET_ACK_VALUE: int = 1 << RESET_ACK_BIT   # = 256


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
        # 2026-09-11: read_status() (called continuously every ~100-200ms by
        # the background safety poll loop) and _pulse_reset_sync() (called
        # on-demand by send_reset_acknowledge()) both reach self._client via
        # run_in_executor, meaning they can run on DIFFERENT worker threads
        # AT THE SAME TIME. A raw Modbus TCP client is not safe to use
        # concurrently from two threads on one connection -- a write from
        # one thread and a read from another can interleave on the wire,
        # and one call can consume the other's response. Found live: a
        # reset-acknowledge pulse reported success (no Modbus error) but
        # never actually took effect, while running against real hardware
        # with the poll loop hammering the same connection every 100-200ms
        # the whole time -- a standalone, single-threaded test script with
        # nothing else touching the connection worked every time. This lock
        # serializes every self._client call so a read and a write (or two
        # of either) can never be in flight on the wire simultaneously.
        self._client_lock = threading.Lock()

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
        # Same lock as reads/pulses (see __init__) -- so close() can't yank
        # the socket out from under an in-flight read or pulse.
        with self._client_lock:
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

        # Serialized against _pulse_reset_sync() -- see _client_lock's
        # comment in __init__. Held only around the actual wire call, not
        # the decoding below, so this poll (runs every 100-200ms) never
        # blocks noticeably long even while a pulse is in flight.
        with self._client_lock:
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
        contactor_closed   = bit(_BIT_CONTACTOR)          # == enable_system, see its comment
        reset_available    = bit(_BIT_RESET_AVAIL)
        safety_ok          = bit(_BIT_SAFETY_OK)          # == p_stop_enabled, see its comment
        edm_okay           = bit(_BIT_EDM_OKAY)
        p_stop_reset_ready = bit(_BIT_P_STOP_RESET_READY)

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
            # 2026-09-11 diagnostic addition -- see _BIT_CONTACTOR's comment:
            # enable_system MUST be 1 before a light-curtain-only recovery
            # (send_reset_acknowledge()) can ever succeed, regardless of how
            # long the curtain has been clear. If a reset attempt fails,
            # check these three in the log line right around it.
            enable_system=contactor_closed,
            edm_okay=edm_okay,
            p_stop_reset_ready=p_stop_reset_ready,
        )

        return {
            "any_fault_active":      any_fault_active,
            "light_curtain_tripped": light_curtain_tripped,
            "estop_active":          estop_active,
            "estop_button_index":    estop_button_index,
            "contactor_closed":      contactor_closed,
            "reset_available":       reset_available,
            "enable_system":         contactor_closed,
            "edm_okay":              edm_okay,
            "p_stop_reset_ready":    p_stop_reset_ready,
        }

    # ------------------------------------------------------------------
    # Protective-stop reset acknowledge
    # ------------------------------------------------------------------

    async def send_reset_acknowledge(self) -> None:
        """
        Pulse the protective-stop reset-acknowledge bit to release the
        light curtain latch. Only called when light curtain is physically
        clear AND operator has pressed CONTINUE on the HMI.

        RESET_ACK_REG/BIT confirmed live on real hardware 2026-09-11 -- see
        the comment above RESET_ACK_REG for the full story (OQ-23 resolved).
        """
        if RESET_ACK_REG is None:
            log.warning(
                "flexi.reset_ack.no_op",
                reason="RESET_ACK_REG not set — ask David for OQ-23 write address",
            )
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._pulse_reset_sync)

    # How long to hold the reset-acknowledge bit high before clearing it.
    # 2026-09-11: was 0.1s (100ms) -- the vendor manual's documented
    # MINIMUM valid pulse width ("100ms-30s"), with zero margin. Widened
    # for real-world headroom: the standalone test that first confirmed
    # this mechanism used 200ms in complete isolation (nothing else on the
    # connection) and worked; the real app's 100ms attempt, running under
    # real safety-poll/robot/camera contention, did not. Also see
    # _client_lock's comment -- that's likely the bigger factor, but this
    # costs nothing to widen too.
    RESET_PULSE_HOLD_S = 0.5

    def _pulse_reset_sync(self) -> None:
        import time
        c = self._client
        if c is None:
            raise RuntimeError("Flexi not connected")
        # Held for the ENTIRE pulse (both writes + the hold in between), not
        # just each individual call -- see _client_lock's comment in
        # __init__. The point is that nothing (in particular the safety
        # poll loop's read, which runs every 100-200ms) can interleave on
        # the wire while this register is deliberately sitting at a
        # non-zero value waiting to be cleared.
        with self._client_lock:
            r = c.write_register(address=RESET_ACK_REG, value=RESET_ACK_VALUE)
            if r.isError():
                raise RuntimeError(f"Flexi reset-ack write failed: {r}")
            time.sleep(self.RESET_PULSE_HOLD_S)
            c.write_register(address=RESET_ACK_REG, value=0)
        log.info("flexi.reset_ack_sent", register=RESET_ACK_REG, bit=RESET_ACK_BIT,
                  hold_s=self.RESET_PULSE_HOLD_S)
