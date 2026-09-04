"""Epson RC+ 7.0 Remote Ethernet driver — controller-level lifecycle control
(login, status, reset, motors on/off, start/stop the SPEL+ program).

Distinct from cib.drivers.robot, which speaks sort_server.prg's own custom
PICK/DROP/MOVE_IMAGE protocol for the actual pick-and-place motion work.
This driver only handles the supervisory layer around that: is the
controller ready, is there a fault to clear, is the program running.

Held open for as long as the task it started needs to keep running (2026-
08-30 real-hardware finding, reversed from an earlier connect-briefly-then-
close design): closing this connection right after Start() reliably killed
the task within seconds, every time, regardless of what else was or wasn't
happening on the network -- mirrors the already-documented "a fresh Login
while Main is running stops the task" behavior. This session appears to be
the task's actual supervisor for as long as it's meant to run, the same way
RC+'s own persistent connection is when a human presses Run (which never
hits this, because that connection never closes either). See
cib/core/orchestrator.py's _remote_ethernet_restart_task() and
self._robot_remote_connected for how the orchestrator tracks this.
"""
