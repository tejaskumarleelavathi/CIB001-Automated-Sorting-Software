' test_sequence.prg -- Full hardware test sequence via TCP
'
' Points (define in RC+ Points editor):
'   P0 -- Home:          X=234.490, Y=75.916,  Z=-4.677,  U=201.510
'   P1 -- Pipette test:  X=44.917,  Y=246.519, Z=-61.119, U=201.510
'
' Outputs:  On 12 / Off 12 = ring light,  On 4 / Off 4 = pipette down/up
' Inputs:   Sw(22) = pipette down sensor,  Sw(21) = pipette up sensor
' Speed:    5% -- very slow. Hit e-stop if anything looks wrong.
'
' TCP commands:
'   HOME, MOVE_TEST, RING_ON, RING_OFF,
'   PIPETTE_DOWN, PIPETTE_UP, CHECK_IN 22, CHECK_IN 21, BYE

Function main
    String cmd$

    Speed 5
    Accel 10, 10

    OpenNet #201 As Server
    Print "Waiting for connection on port 2000..."
    WaitNet #201
    Print "Python connected."

    Do
        Line Input #201, cmd$

        If cmd$ = "HOME" Then
            Motor On
            Go P0
            Print #201, "OK HOME"

        ElseIf cmd$ = "MOVE_TEST" Then
            Go P1
            Print #201, "OK MOVE_TEST"

        ElseIf cmd$ = "RING_ON" Then
            On 12
            Print #201, "OK RING_ON"

        ElseIf cmd$ = "RING_OFF" Then
            Off 12
            Print #201, "OK RING_OFF"

        ElseIf cmd$ = "PIPETTE_DOWN" Then
            On 4
            Print #201, "OK PIPETTE_DOWN"

        ElseIf cmd$ = "PIPETTE_UP" Then
            Off 4
            Print #201, "OK PIPETTE_UP"

        ElseIf cmd$ = "CHECK_IN 22" Then
            If Sw(22) = 1 Then
                Print #201, "IN 22 = 1"
            Else
                Print #201, "IN 22 = 0"
            EndIf

        ElseIf cmd$ = "CHECK_IN 21" Then
            If Sw(21) = 1 Then
                Print #201, "IN 21 = 1"
            Else
                Print #201, "IN 21 = 0"
            EndIf

        ElseIf cmd$ = "BYE" Then
            Print #201, "OK BYE"
            Exit Do

        Else
            Print #201, "UNKNOWN"
        EndIf

        Wait 0.01

    Loop

    CloseNet #201
    Print "Connection closed."
Fend
