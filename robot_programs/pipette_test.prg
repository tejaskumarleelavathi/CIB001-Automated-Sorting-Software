'' pipette_test.prg
'' =============================================================================
'' Hardware test: click-to-pick pipette test.
''
'' DO NOT edit sort_server.prg — load THIS program instead for pipette tests.
''
'' Commands accepted from Python on port 2000:
''   STATUS            -> OK READY
''   HOME              -> ensure pipette up -> go home -> OK HOME
''   MOVE_TO x,y       -> ensure pipette up -> travel to (x,y) at pick Z -> OK AT_POSITION x,y
''   ASPIRE            -> pipette down -> wait In22 -> motor EX G2 -> pipette up -> wait In20 -> OK ASPIRATED
''   DROP              -> pipette down -> wait In22 -> motor EX G1 -> pipette up -> wait In20 -> OK DROPPED
''   BYE               -> close connection, wait for next connection
''
'' Digital I/O:
''   Output  4  ON   -> pipette pneumatic extends DOWN
''   Output  4  OFF  -> pipette pneumatic retracts UP
''   Input  20  ON   -> pipette is fully UP   (safe to move robot)
''   Input  22  ON   -> pipette is fully DOWN (safe to aspirate/drop)
''
'' Motor: Novanta LMDAE572C at 172.16.9.60:503
''   EX G1 -> drop   / push forward (+50000 steps)
''   EX G2 -> aspire / draw back   (-50000 steps)
''   Motor signals completion with ">" prompt.
''   G1 and G2 must already be flashed (run linear_control.py upload first).
''
'' Z heights (mm) — must match sort_server.prg:
''   Z_SAFE = -47.700   safe travel height between XY positions
''   Z_PICK = -58.660   pick height: robot stops here, pipette extends to bug
''
'' Safety guarantees enforced by this program:
''   1. Pipette is NEVER extended while robot is moving.
''   2. MOVE_TO blocks until Input 20 confirms pipette is up.
''   3. ASPIRE / DROP block until Input 22 confirms pipette is down before motor.
''   4. ASPIRE / DROP block until Input 20 confirms pipette is up before returning OK.
''   5. On disconnect: Output 4 is forced OFF (pipette retracted).
'' =============================================================================

Function Main
    pipette_test
Fend


Function pipette_test
    String cmd$
    Real rx, ry
    Integer sp1, sp2

    Motor On
    Power High
    Speed 5
    Accel 5, 5

server_start:
    OpenNet #201 As Server
    WaitNet #201
    Print "pipette_test: Python connected"

    Do
        OnErr GoTo client_gone

        Line Input #201, cmd$

        OnErr GoTo 0
        cmd$ = Trim$(cmd$)

        ' ── STATUS ────────────────────────────────────────────────────────────
        If Left$(cmd$, 6) = "STATUS" Then
            Print #201, "OK READY"

        ' ── HOME ──────────────────────────────────────────────────────────────
        ElseIf Left$(cmd$, 4) = "HOME" Then
            Call EnsurePipetteUp
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            Go P0
            Print #201, "OK HOME"

        ' ── MOVE_TO x,y ───────────────────────────────────────────────────────
        ' Pipette stays UP during travel. Only lowers on ASPIRE or DROP.
        ' Blocks until Input 20 confirms pipette is up before any movement.
        ElseIf Left$(cmd$, 8) = "MOVE_TO " Then
            Call EnsurePipetteUp
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            ' Rise to safe travel Z if currently at pick height
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            ' Travel to target XY at safe Z, then descend to pick height
            Go XY(rx, ry, -47.700, CU(Here))
            Move XY(rx, ry, -58.660, CU(Here))
            Print #201, "OK AT_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ' ── ASPIRE ────────────────────────────────────────────────────────────
        ' Full sequence: pipette down -> wait In22 -> motor EX G2 -> pipette up -> wait In20
        ElseIf Left$(cmd$, 6) = "ASPIRE" Then
            Print "  ASPIRE: lowering pipette (Out4 ON)"
            On 4
            Call WaitPipetteDown
            Call MotorAspire
            Print "  ASPIRE: retracting pipette (Out4 OFF)"
            Off 4
            Call WaitPipetteUp
            Print #201, "OK ASPIRATED"

        ' ── DROP ──────────────────────────────────────────────────────────────
        ' Full sequence: pipette down -> wait In22 -> motor EX G1 -> pipette up -> wait In20
        ElseIf Left$(cmd$, 4) = "DROP" Then
            Print "  DROP: lowering pipette (Out4 ON)"
            On 4
            Call WaitPipetteDown
            Call MotorDrop
            Print "  DROP: retracting pipette (Out4 OFF)"
            Off 4
            Call WaitPipetteUp
            Print #201, "OK DROPPED"

        ' ── BYE ───────────────────────────────────────────────────────────────
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
    ' Safety: always retract pipette on disconnect
    Off 4
    CloseNet #201
    CloseNet #202
    Print "pipette_test: Python disconnected — pipette retracted. Waiting for reconnect..."
    GoTo server_start

    Motor Off
Fend


' =============================================================================
' EnsurePipetteUp
' Check Input 20. If pipette is already up, return immediately.
' If it is not up, wait until Input 20 turns ON before returning.
' Call this before every MOVE_TO and HOME to guarantee safe travel.
' =============================================================================
Function EnsurePipetteUp
    If Sw(20) = 1 Then
        ' Already up — nothing to do
    Else
        Print "  EnsurePipetteUp: pipette not up, waiting for In20..."
        Do
            If Sw(20) = 1 Then Exit Do
            Wait 0.05
        Loop
        Print "  EnsurePipetteUp: pipette UP confirmed."
    EndIf
Fend


' =============================================================================
' WaitPipetteDown
' Block until Input 22 turns ON (pipette fully extended to pick position).
' Call this immediately after turning On 4 to confirm full extension before
' commanding the motor to aspirate or drop.
' =============================================================================
Function WaitPipetteDown
    Print "  WaitPipetteDown: waiting for In22 (pipette fully down)..."
    Do
        If Sw(22) = 1 Then Exit Do
        Wait 0.05
    Loop
    Print "  WaitPipetteDown: pipette DOWN confirmed."
Fend


' =============================================================================
' WaitPipetteUp
' Block until Input 20 turns ON (pipette fully retracted).
' Call this immediately after turning Off 4 to confirm full retraction
' before returning OK to Python.
' =============================================================================
Function WaitPipetteUp
    Print "  WaitPipetteUp: waiting for In20 (pipette fully up)..."
    Do
        If Sw(20) = 1 Then Exit Do
        Wait 0.05
    Loop
    Print "  WaitPipetteUp: pipette UP confirmed."
Fend


' =============================================================================
' MotorAspire
' Open TCP connection to Novanta motor, send EX G2 (draw back = aspirate),
' poll for ">" (move complete) or "?" (error), close connection.
' =============================================================================
Function MotorAspire
    String resp$
    Print "  MotorAspire: connecting to motor at 172.16.9.60:503..."
    SetNet #202, "172.16.9.60", 503, CR, NONE, 0
    OpenNet #202 As Client
    WaitNet #202
    Print "  MotorAspire: sending EX G2 (aspire / draw back)..."
    Print #202, "EX G2"
    Do
        If ChkNet(202) > 0 Then
            Input #202, resp$
            If InStr(resp$, ">") > 0 Then
                Print "  MotorAspire: motor move complete."
                Exit Do
            EndIf
            If InStr(resp$, "?") > 0 Then
                Print "  MotorAspire: [ERROR] motor returned error prompt."
                Exit Do
            EndIf
        EndIf
        Wait 0.1
    Loop
    CloseNet #202
Fend


' =============================================================================
' MotorDrop
' Open TCP connection to Novanta motor, send EX G1 (push forward = drop),
' poll for ">" (move complete) or "?" (error), close connection.
' =============================================================================
Function MotorDrop
    String resp$
    Print "  MotorDrop: connecting to motor at 172.16.9.60:503..."
    SetNet #202, "172.16.9.60", 503, CR, NONE, 0
    OpenNet #202 As Client
    WaitNet #202
    Print "  MotorDrop: sending EX G1 (drop / push forward)..."
    Print #202, "EX G1"
    Do
        If ChkNet(202) > 0 Then
            Input #202, resp$
            If InStr(resp$, ">") > 0 Then
                Print "  MotorDrop: motor move complete."
                Exit Do
            EndIf
            If InStr(resp$, "?") > 0 Then
                Print "  MotorDrop: [ERROR] motor returned error prompt."
                Exit Do
            EndIf
        EndIf
        Wait 0.1
    Loop
    CloseNet #202
Fend
