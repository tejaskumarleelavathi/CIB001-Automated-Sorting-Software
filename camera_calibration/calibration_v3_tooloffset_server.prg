' calibration_v3_tooloffset_server.prg — Overhead calibration WITH Tool 1 active
'
' Same command set as calibration_v2_server.prg, but activates Tool 1 (the
' measured EoA camera offset, cx=17.1876 cy=-77.0233) at startup — same as
' sort_server.prg does for production. This means WHERE / MOVETO / MOVE_U all
' report and target the CAMERA position (tool point), not the raw wrist.
'
' WHY THIS EXISTS
'   The original overhead homography (calibration_overhead.npy, 2026-06-29) was
'   built using calibration_server.prg / calibration_v2_server.prg, BEFORE the
'   tool offset existed — so it maps pixel -> WRIST position. Production
'   (sort_server.prg) now activates Tool 1 for every MOVE_IMAGE, which
'   interprets that same number as a CAMERA target instead, causing a
'   constant ~79mm position error. This program redoes the grid-scan with
'   Tool 1 active so the new H maps pixel -> CAMERA position directly,
'   matching how MOVE_IMAGE actually uses it. See step2_robot_grid_scan_tool_offset.py.
'
' Do NOT run sort_server.prg / calibration_server.prg / calibration_v2_server.prg
' at the same time as this one.
'
' All commands (sent from Python on port 2000):
'   STATUS          -> OK STATUS READY
'   HOME            -> lifts to safe Z if needed, goes to P0
'   WHERE           -> OK WHERE <x> <y> <z> <u>      (x,y = CAMERA position, Tool 1 active)
'   MOVETO x,y      -> moves camera to (x,y), keeps Z and U unchanged
'   MOVE_U x,y,u,hand -> moves camera to (x,y) at wrist angle u, keeps Z.
'                     hand is "L" or "R" -- explicit /L or /R qualifier
'                     (2026-08-26, real-hardware finding elsewhere in this
'                     project: a bare Go without /L /R can silently land
'                     in the wrong hand even when starting from the right
'                     one -- see test_plate_station_positions.prg's own
'                     history for the full story). Required, not optional.
'   DROP_TEST       -> moves the PIPETTE (Tool 2) to Station 3's H12 well,
'                     Lefty, U=200.023 (left_home's U -- per operator: a
'                     quadrant-pick U like 86.053 is not safe to drop at,
'                     dropping must always be back at the home U). Pure
'                     position, no actuation. Restores Tool 1 afterward so
'                     MOVE_U quadrant clicks keep working right after.
'   RING_ON         -> turns ring light on  (Output 12)
'   RING_OFF        -> turns ring light off (Output 12)
'   BYE             -> closes connection
'
' Z is NEVER changed by this server. Speed locked at 10% for safety.

Function Main
    calib_v3_server
Fend

Function calib_v3_server
    String cmd$
    String coord_str$
    String hand$
    Real rx, ry, ru
    Integer comma_pos
    Integer comma1, comma2, comma3

    Motor On
    ' Explicitly clear CP mode -- 2026-08-26 real-hardware finding: CP does
    ' NOT auto-reset except at controller startup/motor-on/task end, so it
    ' can persist from whatever ran before this task (error 4037 seen from
    ' calib_tool.prg, a different file in this project, left it ON). CP
    ' blending requires staying in the same hand throughout -- it cannot
    ' blend through a Righty<->Lefty change, which GO_HOME_LEFT/MOVE_U /L
    ' need to do. Defensive: clears it regardless of what left it on.
    CP Off
    Off 4                                    ' pipette starts retracted -- safety, matches sort_server.prg
    TLSet 1, XY(17.1876, -77.0233, 0, 0)   ' EoA camera offset from U axis (measured 2026-07-08)
    TLSet 2, XY(-22.8124, -77.5233, 0, 0)  ' Pipette offset -- same as sort_server.prg
    Tool 1                                   ' activate tool 1 so CX/CY report camera position
    Power Low

    OpenNet #201 As Server
    Print "Calibration V3 (tool offset) server waiting on port 2000..."
    WaitNet #201
    Print "Python connected."

    Speed 10
    Accel 50, 50

    Do
        Line Input #201, cmd$
        cmd$ = Trim$(cmd$)

        ' ── STATUS ────────────────────────────────────────────────────────────
        If UCase$(cmd$) = "STATUS" Then
            Print #201, "OK STATUS READY"

        ' ── HOME (right_home, Righty) ────────────────────────────────────────
        ElseIf UCase$(cmd$) = "HOME" Then
            ' Go, not Move, for this Z-only lift (2026-08-26 real-hardware
            ' finding) -- Move has no Hand parameter at all (confirmed in
            ' the manual) and threw error 4037 ("point attribute...differ
            ' for CP control command") when the current hand was Lefty.
            ' Explicit /R -- 2026-08-26, second real-hardware finding: a
            ' bare Go XY(...) with NO hand does NOT reliably preserve
            ' whatever hand the arm is currently in -- it silently defaults
            ' toward Righty regardless, causing an unwanted Lefty->Righty->
            ' Lefty double-flip here (this lift runs almost every time --
            ' right_home/left_home's real Z, -47.739/-47.743, is always
            ' just below -47.700). Pin it explicitly instead of assuming
            ' "no XY change" means "no hand needed" -- that assumption was
            ' wrong.
            ' Third real-hardware finding: crossing hands directly from an
            ' arbitrary point (e.g. Lefty at some quadrant click) straight
            ' into right home is unpredictable to the operator, even though
            ' it lands correctly -- they want the SAME behavior as the
            ' already-proven home<->home transition (see
            ' test_left_right_home.prg). So if currently Lefty, first
            ' return to left_home (same-hand, already reliable), THEN do
            ' the proven Lefty->Righty home-to-home PTP into right home.
            ' Fourth real-hardware finding: the crossing step must be a
            ' bare "Go P0" with NO separate Z-lift/hand-pin step first --
            ' that separate step was flipping the arm's posture to Righty
            ' IN PLACE at left_home's own X/Y (a Z-only move with /R)
            ' before ever moving toward right home, instead of sweeping
            ' across in the one continuous motion test_left_right_home.prg
            ' proved safe. The Z-lift safety check is only for returning to
            ' the MATCHING home from an arbitrary point (no reorientation
            ' happening there, same hand throughout) -- it must not be
            ' reused for the actual hand-crossing move.
            If Hand = Lefty Then
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /L
                EndIf
                Go left_home
                Go P0
            Else
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /R
                EndIf
                Go P0
            EndIf
            Print #201, "OK HOME"

        ' ── GO_HOME_LEFT (left_home, Lefty) -- 2026-08-26: establishes Lefty
        ' via the named taught point (reliable) before any quadrant MOVE_U
        ' click attempts /L from wherever the arm currently happens to be.
        ' Same fix as HOME above: if currently Righty, first return to
        ' right_home (same-hand), THEN do the proven Righty->Lefty
        ' home-to-home PTP into left home as a bare "Go left_home" with no
        ' separate Z-lift/hand-pin step first (see HOME's comment above --
        ' that step reorients the arm IN PLACE at right_home instead of
        ' sweeping across).
        ElseIf UCase$(cmd$) = "GO_HOME_LEFT" Then
            If Hand = Righty Then
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /R
                EndIf
                Go P0
                Go left_home
            Else
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /L
                EndIf
                Go left_home
            EndIf
            Print #201, "OK GO_HOME_LEFT"

        ' ── WHERE (returns X Y Z U — X/Y are CAMERA position under Tool 1) ─────
        ElseIf UCase$(cmd$) = "WHERE" Then
            Print #201, "OK WHERE " + Str$(CX(Here)) + " " + Str$(CY(Here)) + " " + Str$(CZ(Here)) + " " + Str$(CU(Here))

        ' ── MOVETO x,y (keeps Z and U) ────────────────────────────────────────
        ElseIf Left$(UCase$(cmd$), 6) = "MOVETO" Then
            coord_str$ = Trim$(Mid$(cmd$, 7))
            comma_pos = InStr(coord_str$, ",")
            If comma_pos = 0 Then
                Print #201, "ERR MOVETO bad format - expected MOVETO x,y"
            Else
                rx = Val(Left$(coord_str$, comma_pos - 1))
                ry = Val(Mid$(coord_str$, comma_pos + 1))
                ' Explicit hand, pinned to whatever hand is CURRENTLY active
                ' (queried via Hand, not assumed) -- 2026-08-26, same root
                ' cause as HOME/GO_HOME_LEFT's Z-lift fix above: a bare Go
                ' XY(...) with no hand does not reliably preserve the
                ' current hand, it can silently default toward Righty.
                ' MOVETO's whole contract is "change x,y, nothing else", so
                ' unlike HOME/GO_HOME_LEFT (which WANT a specific hand) this
                ' preserves current hand rather than forcing one.
                If Hand = Lefty Then
                    Go XY(rx, ry, CZ(Here), CU(Here)) /L
                Else
                    Go XY(rx, ry, CZ(Here), CU(Here)) /R
                EndIf
                Print #201, "OK MOVETO " + Str$(CX(Here)) + " " + Str$(CY(Here))
            EndIf

        ' ── MOVE_U x,y,u,hand (keeps Z, changes U, explicit /L or /R) ─────────
        ElseIf Left$(UCase$(cmd$), 6) = "MOVE_U" Then
            coord_str$ = Trim$(Mid$(cmd$, 7))
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            comma3 = InStr(Mid$(coord_str$, comma2 + 1), ",") + comma2
            If comma1 = 0 Or comma2 = comma1 Or comma3 = comma2 Then
                Print #201, "ERR MOVE_U bad format - expected MOVE_U x,y,u,hand"
            Else
                rx = Val(Left$(coord_str$, comma1 - 1))
                ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
                ru = Val(Mid$(coord_str$, comma2 + 1, comma3 - comma2 - 1))
                hand$ = Mid$(coord_str$, comma3 + 1)
                ' Left$(hand$,1) not hand$= -- immune to trailing terminator
                ' garbage, same reasoning as test_plate_station_positions.prg.
                ' Go, not Move -- Move's syntax has no Hand parameter at all
                ' (confirmed in the SPEL+ manual), so it can silently land in
                ' the wrong hand even with /L specified. Go does respect it.
                If Left$(hand$, 1) = "L" Then
                    Go XY(rx, ry, CZ(Here), ru) /L
                Else
                    Go XY(rx, ry, CZ(Here), ru) /R
                EndIf
                Print #201, "OK MOVE_U " + Str$(CX(Here)) + " " + Str$(CY(Here)) + " " + Str$(CU(Here))
            EndIf

        ' ── DROP_TEST -- pipette (Tool 2) to Station 3's H12, Lefty, U=200.023
        ' Position only, no actuation. Restores Tool 1 after so quadrant
        ' MOVE_U clicks keep working without needing to restart.
        ElseIf UCase$(cmd$) = "DROP_TEST" Then
            Off 4
            ' Inlined, not a separate Function -- sort_server.prg already
            ' defines EnsurePipetteUp with this exact name, and if both
            ' files are in the same RC+ project (shared function
            ' namespace), a duplicate name fails to compile.
            If Sw(20) = 1 Then
                ' Already up -- nothing to do
            Else
                Do
                    If Sw(20) = 1 Then Exit Do
                    Wait 0.05
                Loop
            EndIf
            Tool 2
            ' Power High for Tool 2/pipette reach, same as sort_server.prg --
            ' the rest of this program runs Power Low (fine for Tool 1/camera
            ' moves), but Station 3 is a genuinely long reach and Low risks
            ' an underpowered-torque error that camera-only use never hit.
            Power High
            Go left_home
            Go XY(-255.434, 184.254, -47.700, 200.023) /L
            Go XY(-255.434, 184.254, -58.660, 200.023) /L
            Power Low
            Tool 1
            Print #201, "OK DROP_TEST"

        ' ── RING_ON ───────────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "RING_ON" Then
            On 12
            Print #201, "OK RING_ON"

        ' ── RING_OFF ──────────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "RING_OFF" Then
            Off 12
            Print #201, "OK RING_OFF"

        ' ── BYE ───────────────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "BYE" Then
            Print #201, "OK BYE"
            CloseNet #201
            Exit Do

        Else
            Print #201, "ERR UNKNOWN: " + cmd$
        EndIf

    Loop

Fend
