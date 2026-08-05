' calibration_v2_server.prg — Extended SPEL+ TCP server for full calibration
'
' USE THIS instead of calibration_server.prg when running step2_full_calibration.py.
' Do NOT run sort_server.prg or calibration_server.prg at the same time.
'
' NEW vs calibration_server.prg:
'   WHERE now returns X Y Z U  (adds U wrist angle)
'   RING_ON / RING_OFF         ring light on Output 12
'   MOVE_U x,y,u               move to (x,y) at specific U angle, keep Z
'
' All commands (sent from Python on port 2000):
'   STATUS          -> OK STATUS READY
'   WHERE           -> OK WHERE <x> <y> <z> <u>
'   MOVETO x,y      -> moves to (x,y), keeps Z and U unchanged
'   MOVE_U x,y,u    -> moves to (x,y) at wrist angle u, keeps Z
'   RING_ON         -> turns ring light on  (Output 12)
'   RING_OFF        -> turns ring light off (Output 12)
'   BYE             -> closes connection
'
' Z is NEVER changed by this server. Speed locked at 10% for safety.

Function Main
    calib_v2_server
Fend

Function calib_v2_server
    String cmd$
    String coord_str$
    Real rx, ry, ru
    Integer comma_pos
    Integer comma1, comma2

    Motor On
    Power Low

    OpenNet #201 As Server
    Print "Calibration V2 server waiting on port 2000..."
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

        ' ── WHERE (returns X Y Z U) ───────────────────────────────────────────
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
