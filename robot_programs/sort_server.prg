Function Main
    sort_server
Fend

Function sort_server
    String cmd$
    Real rx, ry, cur_u
    Integer sp1, sp2

    Motor On
    Power High
    Speed 5
    Accel 5, 5

    OpenNet #201 As Server
    WaitNet #201

    Do
        Line Input #201, cmd$
        cmd$ = Trim$(cmd$)

        If Left$(cmd$, 6) = "STATUS" Then
            Print #201, "OK READY"

        ElseIf Left$(cmd$, 4) = "HOME" Then
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            Go P0
            Print #201, "OK HOME"

        ElseIf Left$(cmd$, 11) = "MOVE_IMAGE " Then
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            cur_u = CU(Here)
            Go XY(rx, ry, -47.700, cur_u)
            Move XY(rx, ry, -58.660, cur_u)
            Print #201, "OK IMAGE_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ElseIf Left$(cmd$, 7) = "RING_ON" Then
            On 12
            Print #201, "OK RING_ON"

        ElseIf Left$(cmd$, 8) = "RING_OFF" Then
            Off 12
            Print #201, "OK RING_OFF"

        ElseIf Left$(cmd$, 3) = "BYE" Then
            Print #201, "OK BYE"
            CloseNet #201
            Exit Do

        Else
            Print #201, "ERR UNKNOWN " + cmd$

        EndIf

    Loop

    Motor Off

Fend
