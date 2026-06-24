' test_autonomous.prg -- Robot handles full pick sequence autonomously
'
' Python sends just: HOME or PICK
' Robot does everything: move, ring light, pipette, sensor checks, return home
' Python only receives the final result.
'
' Points:
'   P0 -- Home:         X=234.490, Y=75.916,  Z=-4.677,  U=201.510
'   P1 -- Pick pos:     X=44.917,  Y=246.519, Z=-61.119, U=201.510
'
' Commands:
'   HOME  -- Motor On, go to P0, reply OK HOME
'   PICK  -- Full sequence at P1, reply PICK_OK or PICK_FAIL details
'   BYE   -- Close connection

Function main
    String cmd$
    Integer downOk
    Integer upOk

    Speed 5
    Accel 10, 10

    OpenNet #201 As Server
    Print "Waiting for connection..."
    WaitNet #201
    Print "Python connected."

    Do
        Line Input #201, cmd$

        If cmd$ = "HOME" Then
            Motor On
            Go P0
            Print #201, "OK HOME"

        ElseIf cmd$ = "PICK" Then
            ' ── Step 1: Move to pick position ────────────────────────────
            Go P1

            ' ── Step 2: Ring light ON ─────────────────────────────────────
            On 12
            Wait 5

            ' ── Step 3: Ring light OFF ────────────────────────────────────
            Off 12

            ' ── Step 4: Pipette DOWN ──────────────────────────────────────
            On 4
            Wait 1

            ' ── Step 5: Check pipette down sensor ────────────────────────
            If Sw(22) = 1 Then
                downOk = 1
            Else
                downOk = 0
            EndIf

            Wait 2

            ' ── Step 6: Pipette UP ────────────────────────────────────────
            Off 4
            Wait 1

            ' ── Step 7: Check pipette up sensor ──────────────────────────
            If Sw(21) = 1 Then
                upOk = 1
            Else
                upOk = 0
            EndIf

            ' ── Step 8: Return home ───────────────────────────────────────
            Go P0

            ' ── Step 9: Send result to Python ────────────────────────────
            If downOk = 1 And upOk = 1 Then
                Print #201, "PICK_OK DOWN=1 UP=1"
            ElseIf downOk = 0 And upOk = 1 Then
                Print #201, "PICK_FAIL DOWN=0 UP=1"
            ElseIf downOk = 1 And upOk = 0 Then
                Print #201, "PICK_FAIL DOWN=1 UP=0"
            Else
                Print #201, "PICK_FAIL DOWN=0 UP=0"
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
