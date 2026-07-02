'' test_motor_comms.prg
'' --------------------------------------------------------------
'' Test program: robot opens TCP to linear motor and commands it.
'' Load this in RC+ alongside sort_server.prg (do NOT replace it).
'' Run via F5 in RC+, then trigger from hardware_tests/test_robot_motor_direct.py
''
'' Commands accepted from Python:
''   ASPIRE  -> robot connects to motor, sends EX G2 (draw back), replies OK ASPIRATED
''   DROP    -> robot connects to motor, sends EX G1 (push forward), replies OK DROPPED
''   BYE     -> close connection cleanly
''
'' Motor: Novanta LMDAE572C at 172.16.9.60:503
'' Programs G1/G2 must already be flashed (run linear_control.py upload first).
''
'' Key: SetNet configures client params BEFORE OpenNet As Client — required in SPEL+.
'' --------------------------------------------------------------

Function Main
    test_motor_comms
Fend

Function test_motor_comms
    String cmd$
    String response$

    Print "test_motor_comms: starting TCP server on port 2000"

server_start:
    OpenNet #201 As Server
    WaitNet #201
    Print "test_motor_comms: Python connected"

    Do
        OnErr GoTo client_gone

        Line Input #201, cmd$

        OnErr GoTo 0
        cmd$ = Trim$(cmd$)

        ' -- ASPIRE COMMAND --------------------------------------------------
        If Left$(cmd$, 6) = "ASPIRE" Then
            Print "  -> Connecting to motor 172.16.9.60:503..."
            SetNet #202, "172.16.9.60", 503, CR, NONE, 0
            OpenNet #202 As Client
            WaitNet #202

            Print "  -> Sending EX G2 (aspire / draw back)"
            Print #202, "EX G2"

            ' Poll for > (ready) or ? (error) from motor.
            ' G2 program has H inside so motor won't send > until move is done.
            Do
                If ChkNet(202) > 0 Then
                    Input #202, response$
                    If InStr(response$, ">") > 0 Then Exit Do
                    If InStr(response$, "?") > 0 Then
                        Print "  -> [ERROR] Motor returned error prompt"
                        Exit Do
                    EndIf
                EndIf
                Wait 0.1
            Loop

            CloseNet #202
            Print "  -> Motor move complete"
            Print #201, "OK ASPIRATED"

        ' -- DROP COMMAND ----------------------------------------------------
        ElseIf Left$(cmd$, 4) = "DROP" Then
            Print "  -> Connecting to motor 172.16.9.60:503..."
            SetNet #202, "172.16.9.60", 503, CR, NONE, 0
            OpenNet #202 As Client
            WaitNet #202

            Print "  -> Sending EX G1 (drop / push forward)"
            Print #202, "EX G1"

            Do
                If ChkNet(202) > 0 Then
                    Input #202, response$
                    If InStr(response$, ">") > 0 Then Exit Do
                    If InStr(response$, "?") > 0 Then
                        Print "  -> [ERROR] Motor returned error prompt"
                        Exit Do
                    EndIf
                EndIf
                Wait 0.1
            Loop

            CloseNet #202
            Print "  -> Motor move complete"
            Print #201, "OK DROPPED"

        ' -- BYE COMMAND -----------------------------------------------------
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
    CloseNet #202
    Print "test_motor_comms: Python disconnected. Resetting..."
    GoTo server_start

Fend
