Function Main
    sort_server
Fend

Function sort_server
    String cmd$
    Real rx, ry, cur_u
    Integer sp1, sp2

    Motor On
    TLSet 1, XY(17.1876, -77.0233, 0, 0)   ' EoA camera offset from U axis (measured 2026-07-08)
    Tool 1                                   ' activate tool 1 so CX/CY report camera position
    Power High
    Speed 5
    Accel 5, 5

server_start:
    OpenNet #201 As Server
    WaitNet #201

    Do
        OnErr GoTo client_gone

        Line Input #201, cmd$

        OnErr GoTo 0
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
            GoTo server_start

        Else
            Print #201, "ERR UNKNOWN " + cmd$

        EndIf

    Loop

client_gone:
    OnErr GoTo 0
    EResume client_gone_resume
client_gone_resume:
    CloseNet #201
    GoTo server_start

    Motor Off

Fend
