"""
Real Logic PLC driver — AutomationDirect P1-622 via ModbusTCP.

STATUS LIGHT — register map confirmed 2026-06-18 via test_status_light.py:
  Reg 0  (40001) Mode             0=Off 1=Steady 2=Flash 3=TwoColorFlash
                                   4=50/50 5=Rotate 6=Chase 7=IntensitySweep
                                   8=ColorSweep 9=Sequence 10=Wave 11=DoubleWave
  Reg 1  (40002) Animation Pattern
  Reg 2  (40003) Animation Direction   0=CCW 1=CW
  Reg 3  (40004) Animation Speed       0=Slow 1=Medium 2=Fast
  Reg 4  (40005) Sequence Value
  Reg 5  (40006) Primary Color         0=Green 1=Red 2=Orange 3=Amber 4=Yellow
                                        5=LimeGreen 6=SpringGreen 7=Cyan 8=SkyBlue
                                        9=Blue 10=Violet 11=Magenta 12=Rose 13=White
  Reg 6  (40007) Primary Intensity     0=High 1=Medium 2=Low 3=N/A 4=Off
  Reg 7  (40008) Secondary Color       (same values as Primary Color)
  Reg 8  (40009) Secondary Intensity   (same values as Primary Intensity)
  Coil 0 (00001) Demo Mode             0=off 1=on

WORK LIGHT — register address OQ-11 PENDING.
  set_work_light() is a no-op until David confirms the register address.

EVENT LOG — register address OQ-11 PENDING.
  read_event_log() returns [] until David confirms the register address.

Device: 172.16.9.11, port 502, ModbusTCP
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from pymodbus.client import ModbusTcpClient

log = structlog.get_logger(__name__)

# ── Hardware constants ────────────────────────────────────────────────────────

PLC_IP   = "172.16.9.11"
PLC_PORT = 502

# Status light holding registers (0-based → Modbus address 40001 = offset 0)
REG_MODE       = 0   # 40001  light mode
REG_PATTERN    = 1   # 40002  animation pattern
REG_DIRECTION  = 2   # 40003  animation direction
REG_SPEED      = 3   # 40004  animation speed
REG_SEQ        = 4   # 40005  sequence value (unused for simple modes)
REG_COLOR_PRI  = 5   # 40006  primary colour
REG_INTENS_PRI = 6   # 40007  primary intensity
REG_COLOR_SEC  = 7   # 40008  secondary colour
REG_INTENS_SEC = 8   # 40009  secondary intensity

COIL_DEMO             = 0   # 00001  demo mode (turn off after use)
COIL_STATUSLIGHT_SET  = 4   # 00005  pulse high→low to apply new register values

# Colour codes
_COLOR = {
    "green": 0, "red": 1, "orange": 2, "amber": 3, "yellow": 4,
    "lime": 5, "spring": 6, "cyan": 7, "sky": 8, "blue": 9,
    "violet": 10, "magenta": 11, "rose": 12, "white": 13,
}

# Light mode codes
_MODE = {
    "off": 0, "steady": 1, "flash": 2, "twocolor": 3,
    "5050": 4, "rotate": 5, "chase": 6, "sweep": 7,
    "colorsweep": 8, "sequence": 9, "wave": 10, "doublewave": 11,
}

# Status-light colour → (mode, primary_color, primary_intensity, secondary_color, anim_speed)
# GREEN  = steady green   — system running normally
# AMBER  = steady amber   — paused / cycle-stop / waiting
# RED    = flashing red   — fault / e-stop / connection loss
# OFF    = all off
_LIGHT_CONFIG: dict[str, tuple[int, int, int, int, int]] = {
    #               mode              pri_col  pri_int  sec_col  speed
    "green":  (_MODE["steady"], _COLOR["green"],  0, _COLOR["green"],  1),
    "amber":  (_MODE["steady"], _COLOR["amber"],  0, _COLOR["amber"],  1),
    "red":    (_MODE["flash"],  _COLOR["red"],    0, _COLOR["red"],    1),
    "off":    (_MODE["off"],    _COLOR["green"],  4, _COLOR["green"],  1),
}


class LogicPLCDriver:
    """
    Logic PLC driver — status light (confirmed) + work light / event log stubs.
    All Modbus I/O runs in a thread executor so the async event loop never blocks.
    """

    def __init__(
        self,
        ip: str = PLC_IP,
        port: int = PLC_PORT,
        connect_timeout_s: float = 3.0,
    ) -> None:
        self._ip      = ip
        self._port    = port
        self._timeout = connect_timeout_s
        self._client: ModbusTcpClient | None = None

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
                f"PLC at {self._ip}:{self._port} refused connection. "
                "Check Ethernet cable and P1-622 power."
            )
        self._client = client
        log.info("plc.connected", ip=self._ip, port=self._port)

    async def disconnect(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        log.info("plc.disconnected")

    # ------------------------------------------------------------------
    # Status light  (registers confirmed)
    # ------------------------------------------------------------------

    async def set_status_light(self, colour: str) -> None:
        """
        Set the status light to one of: "green", "amber", "red", "off".
        Raises ValueError for unknown colours.
        """
        colour = colour.lower()
        if colour not in _LIGHT_CONFIG:
            raise ValueError(
                f"Unknown status light colour {colour!r}. "
                f"Valid: {list(_LIGHT_CONFIG)}"
            )
        cfg = _LIGHT_CONFIG[colour]
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_light_sync, colour, cfg)

    def _set_light_sync(self, colour: str, cfg: tuple) -> None:
        mode, pri_col, pri_int, sec_col, speed = cfg
        c = self._client
        if c is None:
            log.error("plc.set_status_light.not_connected", colour=colour)
            return
        errs = []
        for addr, val in [
            (REG_MODE,       mode),
            (REG_COLOR_PRI,  pri_col),
            (REG_INTENS_PRI, pri_int),
            (REG_COLOR_SEC,  sec_col),
            (REG_INTENS_SEC, pri_int),   # match intensity on both channels
            (REG_SPEED,      speed),
        ]:
            r = c.write_register(address=addr, value=val)
            if r.isError():
                errs.append(f"reg{addr}={val}: {r}")
        if errs:
            log.error("plc.set_status_light.write_error", colour=colour, errors=errs)
            return
        # Pulse Statuslight_Set coil high→low so PLC applies the new register values
        import time
        c.write_coil(address=COIL_STATUSLIGHT_SET, value=True)
        time.sleep(0.05)
        c.write_coil(address=COIL_STATUSLIGHT_SET, value=False)
        log.debug("plc.status_light", colour=colour)

    # ------------------------------------------------------------------
    # Work light  (OQ-11 PENDING — no-op until register address confirmed)
    # ------------------------------------------------------------------

    async def set_work_light(self, on: bool) -> None:
        log.warning(
            "plc.set_work_light.not_implemented",
            on=on,
            reason="OQ-11 open — work light register address not yet confirmed by David",
        )

    # ------------------------------------------------------------------
    # Event log  (OQ-11 PENDING — returns [] until register address confirmed)
    # ------------------------------------------------------------------

    async def read_event_log(self) -> list[str]:
        # OQ-11 pending: we don't know which registers hold the PLC event log.
        # Return empty list so the poll loop doesn't fault.  Once David answers
        # OQ-11, replace this stub with the actual Modbus read.
        return []
