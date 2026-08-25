' test_left_right_home.prg — isolated hardware test for the Righty/Lefty
' hand-configuration change between right_home (P0, Righty) and left_home
' (P2, Lefty). Standalone, does not touch sort_server.prg at all.
'
' Go <point> is a PTP move — all joints (including Joint1/Joint2) already
' interpolate together to reach the target pose, arriving simultaneously.
' Speed/Accel below are set very low (5%) specifically so this first test
' is slow enough to watch closely and react to anything unexpected.
'
' Run from RC+'s Run window: select "test_left_right_home" in the
' Function dropdown, then Start. Watch the whole sweep, stay near E-stop.

Function test_left_right_home
    Motor On
    Tool 1

    Speed 5
    Accel 5, 5

    Print "Starting at right_home..."
    Go right_home
    Print "At right_home. Pausing 2s..."
    Wait 2

    Print "Moving to left_home (Righty -> Lefty hand change)..."
    Go left_home
    Print "At left_home. Pausing 2s..."
    Wait 2

    Print "Moving back to right_home (Lefty -> Righty hand change)..."
    Go right_home
    Print "Done. Back at right_home."
Fend
