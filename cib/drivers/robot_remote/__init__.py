"""Epson RC+ 7.0 Remote Ethernet driver — controller-level lifecycle control
(login, status, reset, motors on/off, start/stop the SPEL+ program).

Distinct from cib.drivers.robot, which speaks sort_server.prg's own custom
PICK/DROP/MOVE_IMAGE protocol for the actual pick-and-place motion work.
This driver only handles the supervisory layer around that: is the
controller ready, is there a fault to clear, is the program running.
"""
