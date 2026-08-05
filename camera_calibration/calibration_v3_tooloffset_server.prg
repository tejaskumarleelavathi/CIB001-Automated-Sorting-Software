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
'   MOVE_U x,y,u    -> moves camera to (x,y) at wrist angle u, keeps Z
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
    Real rx, ry, ru
    Integer comma_pos
    Integer comma1, comma2

    Motor On
    TLSet 1, XY(17.1876, -77.0233, 0, 0)   ' EoA camera offset from U axis (measured 2026-07-08)
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

        ' ── MOVE_U x,y,u (keeps Z, changes U) ────────────────────────────────
        ElseIf Left$(UCase$(cmd$), 6) = "MOVE_U" Then
            coord_str$ = Trim$(Mid$(cmd$, 7))
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            If comma1 = 0 Or comma2 = comma1 Then
                Print #201, "ERR MOVE_U bad format - expected MOVE_U x,y,u"
            Else
                rx = Val(Left$(coord_str$, comma1 - 1))
                ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
                ru = Val(Mid$(coord_str$, comma2 + 1))
                Go XY(rx, ry, CZ(Here), ru)
                Print #201, "OK MOVE_U " + Str$(CX(Here)) + " " + Str$(CY(Here)) + " " + Str$(CU(Here))
            EndIf

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
