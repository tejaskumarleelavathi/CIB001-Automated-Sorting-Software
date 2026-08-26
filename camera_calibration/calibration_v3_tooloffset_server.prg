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

        ' ── HOME ──────────────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "HOME" Then
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            Go P0
            Print #201, "OK HOME"

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
                Go XY(rx, ry, CZ(Here), CU(Here))
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
            Call EnsurePipetteUp
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


' =============================================================================
' EnsurePipetteUp -- block until Input 20 confirms the pipette is fully
' retracted, before any XY travel. Duplicated from sort_server.prg (kept
' self-contained here rather than assumed cross-file, since this program
' lives in a different project folder) -- identical, already-proven logic.
' =============================================================================
Function EnsurePipetteUp
    If Sw(20) = 1 Then
        ' Already up -- nothing to do
    Else
        Do
            If Sw(20) = 1 Then Exit Do
            Wait 0.05
        Loop
    EndIf
Fend
