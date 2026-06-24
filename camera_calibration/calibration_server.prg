' calibration_server.prg - SPEL+ TCP server for camera calibration
'
' Load into RC+ 7.0 as a separate task and run (F5).
' Do NOT run robot_server.prg at the same time.
'
' Commands (sent from Python, TCP port 2000):
'   STATUS       -> OK STATUS READY
'   WHERE        -> OK WHERE <x> <y> <z>
'   MOVETO x,y   -> moves to (x,y) keeping Z and U, replies OK MOVETO <x> <y>
'   BYE          -> closes connection
'
' Z and U are NEVER changed. Speed locked at 10% for safety.

Function Main
    calib_server
Fend

Function calib_server
    String cmd$
    String coord_str$
    Real rx, ry
    Integer comma_pos

    Motor On
    Power Low

    OpenNet #201 As Server
    Print "Calibration server waiting on port 2000..."
    WaitNet #201
    Print "Python connected."

    Speed 10
    Accel 50, 50

    Do
        Line Input #201, cmd$
        cmd$ = Trim$(cmd$)

        If UCase$(cmd$) = "STATUS" Then
            Print #201, "OK STATUS READY"

        ElseIf UCase$(cmd$) = "WHERE" Then
            Print #201, "OK WHERE " + Str$(CX(Here)) + " " + Str$(CY(Here)) + " " + Str$(CZ(Here))

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

        ElseIf UCase$(cmd$) = "BYE" Then
            Print #201, "OK BYE"
            CloseNet #201
            Exit Do

        Else
            Print #201, "ERR UNKNOWN: " + cmd$
        EndIf

    Loop

Fend
