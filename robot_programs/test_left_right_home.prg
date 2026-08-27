' test_left_right_home.prg — 2026-08-26: simplified to just go to left_home
' and stop there (was: right_home -> left_home -> right_home round trip,
' see git history for that version). Operator request -- parking here and
' staying lets them jog from left_home as a reference point to measure
' Station 3/4 corners more accurately via RC+'s Jog & Teach panel.
' Standalone, does not touch sort_server.prg at all.
'
' Run from RC+'s Run window: select "test_left_right_home" in the
' Function dropdown, then Start.

Function test_left_right_home
    Motor On
    Tool 1

    Speed 5
    Accel 5, 5

    Print "Moving to left_home..."
    Go left_home
    Print "At left_home. Stopped here -- jog from here to measure Station 3/4 corners."
Fend
