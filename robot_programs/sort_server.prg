' sort_server.prg — production robot TCP server
'
' Commands (sent from Python on port 2000):
'   STATUS               -> OK READY
'   HOME                 -> OK HOME              (ensures pipette up, lifts, goes to P0)
'   MOVE_IMAGE x,y       -> OK IMAGE_POSITION x,y      (Tool 1/camera, holds current U)
'   MOVE_IMAGE_U x,y,u   -> OK IMAGE_POSITION x,y      (Tool 1/camera, explicit U — quadrant selection)
'   MOVE_PIPETTE x,y     -> OK PIPETTE_POSITION x,y    (Tool 2/pipette, holds current U, NO actuation — teaching)
'   MOVE_PIPETTE_U x,y,u -> OK PIPETTE_POSITION x,y    (Tool 2/pipette, explicit U, NO actuation — teaching)
'   PIPETTE_DOWN          -> OK PIPETTE_DOWN            (extend pipette at current XY, no motor — teaching)
'   PIPETTE_UP            -> OK PIPETTE_UP              (retract pipette, no motor — teaching)
'   PICK x,y             -> OK PICKED x,y              (Tool 2/pipette, holds current U, aspirates)
'   PICK_U x,y,u         -> OK PICKED x,y              (Tool 2/pipette, explicit U, aspirates)
'   DROP x,y             -> OK DROPPED x,y             (Tool 2/pipette, holds current U, dispenses)
'   DROP_U x,y,u         -> OK DROPPED x,y             (Tool 2/pipette, explicit U, dispenses)
' DROP/DROP_U pause 3s at travel height above the drop XY before descending —
' operator E-STOP reaction window in case the drop location is wrong.
'   RING_ON / RING_OFF
'   BYE
'
' Two tools:
'   Tool 1 = EoA camera offset (measured 2026-07-08, cx=17.1876, cy=-77.0233)
'   Tool 2 = Pipette offset — used for BOTH PICK and DROP (measured 2026-08-06
'            from David's CAD: camera + (-40, -0.5)). Several X/Y corrections
'            (Tool 2 tweaks, a DROP-only Tool 3, matching plate-config well
'            shifts) were tried and reverted on 2026-08-19 while the real
'            cause was misdiagnosed as a tool-offset error — it was actually
'            eoa_pixel_scale.json being calibrated at an unrecorded U angle
'            (see DEV-14). Fixed with per-quadrant EoA calibration files
'            instead; Tool 2 and the plate config are both back to their
'            true original values. Any further fine correction now lives in
'            Python (cib/core/orchestrator.py), applied only to the vision-
'            computed PICK target — never here, never in the plate config.
' Every command handler explicitly (re)selects the tool it needs — never rely
' on whatever tool a previous command left active.
'
' Pipette I/O safety (ported from pipette_test.prg, proven working):
'   Output  4  ON   -> pipette pneumatic extends DOWN
'   Output  4  OFF  -> pipette pneumatic retracts UP
'   Input  20  ON   -> pipette is fully UP   (safe to move robot)
'   Input  22  ON   -> pipette is fully DOWN (safe to aspirate/drop)
' Pipette is never extended while the robot performs any XY (lateral) travel.
' On disconnect, Output 4 is forced OFF (pipette retracted) so nothing is left
' hanging.
'
' Two-stage controlled descent (David's fix, 2026-08-19 — see DEV-11):
' the pipette mount was physically raised so full pneumatic extension (Output
' 4 ON) can no longer reach the glass dish/well bottom under any software
' condition — a hard mechanical limit, not a calibration value. Because the
' pneumatic alone therefore can no longer reach the specimen, the robot's own
' Z axis (speed/accel-controlled, unlike the on/off pneumatic) now does the
' final approach. Both PICK/PICK_U and DROP/DROP_U now sequence as:
'   1. On 4 (pipette extends — its own now-limited, short stroke)
'   2. WaitPipetteDown (Input 22 confirms full pneumatic extension)
'   3. Move Z only, same XY, down to -62.000 (robot/EOA controlled descent —
'      this is what actually reaches the specimen/well). Runs at SpeedS 10 /
'      AccelS 10,10 (mm/sec — Move is a linear/CP command, controlled by
'      SpeedS/AccelS, NOT Speed/Accel, which only affect Go/PTP commands and
'      do nothing to Move. An earlier attempt at slowing this down used
'      Speed/Accel and had no effect — found and fixed 2026-08-19.) The
'      whole point of this being a robot-controlled move instead of the
'      pneumatic is a controlled approach, so it must run slow. SpeedS/
'      AccelS are restored to the fast baseline (100 / 100,100) immediately
'      after this move — that same fast setting then carries through the
'      retract-up move (step 5) and the final travel move (step 7).
'   4. Aspirate (PICK) or dispense (DROP). DROP still fires here, after the
'      descent, via the original synchronous MotorDrop. PICK diverges as of
'      2026-08-19 (DEV-16): StartMotorAspire fires BEFORE step 3's descent
'      (aspiration begins while the tip is still approaching, not only
'      after it fully arrives — see StartMotorAspire/WaitMotorComplete),
'      with WaitMotorComplete confirming completion right here at step 4
'      before step 5's retract proceeds.
'   5. Move Z only, same XY, back up to -58.660 (robot/EOA retracts FIRST,
'      fast — see step 3)
'   6. Off 4 (pipette retracts SECOND, only once EOA is already clear)
'   7. WaitPipetteUp, then Move to -47.700 travel height (only XY travel move
'      happens after the pipette is confirmed fully up)
' Steps 3 and 5 are Z-only (no XY change) so they never violate "no XY travel
' with the pipette down" — only step 7's travel move does, and it only runs
' after WaitPipetteUp confirms the pipette is clear.
'
' Motor: Novanta LMDAE572C linear actuator at 172.16.9.60:503
'   EX G2 -> aspirate / draw back  (PICK/PICK_U)
'   EX G1 -> drop / push forward   (DROP/DROP_U)
'
' PICK and DROP both target an explicit (x,y) you provide — there is still
' no automatic well-travel (CyclePhase.ROBOT_TO_WELL / PUMP_DISPENSING /
' _get_next_well_coords() in the orchestrator is still a hardcoded stub).
' To empty the pipette right now: send DROP/DROP_U at wherever you want it
' released (e.g. back in the dish, or a waste spot) — it is NOT automatic.

Function Main
    sort_server
Fend

Function sort_server
    String cmd$
    String coord_str$
    Real rx, ry, cur_u, ru
    Integer sp1, sp2
    Integer comma1, comma2

    Motor On
    Off 4    ' pipette always starts retracted (UP) — never DOWN on program start
    TLSet 1, XY(17.1876, -77.0233, 0, 0)    ' EoA camera offset (measured 2026-07-08)
    TLSet 2, XY(-22.8124, -77.5233, 0, 0)   ' Pipette offset (measured 2026-08-06
                                             ' from David's CAD: camera + (-40, -0.5)).
                                             ' REVERTED to this true original value
                                             ' 2026-08-19 — every X/Y correction tried
                                             ' this session (Tool 2 tweaks, a DROP-only
                                             ' Tool 3, well-config shifts) was chasing
                                             ' the wrong root cause. The real bug (see
                                             ' DEV-14) was eoa_pixel_scale.json being
                                             ' calibrated at an unrecorded U angle and
                                             ' applied uniformly at both real quadrant
                                             ' angles — fixed with per-quadrant
                                             ' calibration files. Any remaining fine
                                             ' correction is now applied in Python, to
                                             ' the vision-computed PICK target only
                                             ' (cib/core/orchestrator.py), NOT here and
                                             ' NOT in the plate config — so it can never
                                             ' again leak into DROP the way the Tool 2/
                                             ' well-config approach kept doing.
    Tool 1
    Power High
    Speed 5
    Accel 5, 5
    ' SpeedS/AccelS govern Move (linear/CP) commands — Speed/Accel above only
    ' govern Go (PTP) commands and have NO effect on Move. Every fine-motion
    ' command in this program uses Move, so this is the real "fast/slow"
    ' knob. Set once here as the default "fast" baseline; PICK/PICK_U/
    ' DROP/DROP_U temporarily drop to a slow SpeedS/AccelS for the
    ' controlled final descent only, then restore these same fast values.
    SpeedS 100
    AccelS 100, 100

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
            Call EnsurePipetteUp
            Tool 1
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            Go P0
            Print #201, "OK HOME"

        ElseIf Left$(cmd$, 11) = "MOVE_IMAGE " Then
            Tool 1
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            cur_u = CU(Here)
            Go XY(rx, ry, -47.700, cur_u)
            Move XY(rx, ry, -58.660, cur_u)
            Print #201, "OK IMAGE_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ElseIf Left$(cmd$, 13) = "MOVE_IMAGE_U " Then
            ' MOVE_IMAGE_U x,y,u — same as MOVE_IMAGE but with an explicit U
            ' angle instead of holding whatever U the arm currently has.
            ' Used for quadrant-based U selection (see cib/core/orchestrator.py).
            Tool 1
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            Move XY(rx, ry, -58.660, ru)
            Print #201, "OK IMAGE_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ElseIf Left$(cmd$, 13) = "MOVE_PIPETTE " Then
            ' MOVE_PIPETTE x,y — same as MOVE_PIPETTE_U but holds whatever U
            ' the arm is currently at, instead of requiring an explicit one.
            ' Fine for a plate station reachable at a single constant U.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            cur_u = CU(Here)
            Go XY(rx, ry, -47.700, cur_u)
            Move XY(rx, ry, -58.660, cur_u)
            Print #201, "OK PIPETTE_POSITION " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 15) = "MOVE_PIPETTE_U " Then
            ' MOVE_PIPETTE_U x,y,u — pure positioning move for the pipette
            ' (Tool 2), NO actuation (no Output 4, no motor). Used for
            ' teaching well positions — lets a script position the pipette
            ' precisely for visual confirmation without ever firing aspirate/
            ' dispense. Descends to the same height PICK/DROP use so the
            ' taught position matches what production picking will actually see.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            Move XY(rx, ry, -58.660, ru)
            Print #201, "OK PIPETTE_POSITION " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 12) = "PIPETTE_DOWN" Then
            ' PIPETTE_DOWN — manual actuation only: extend the pipette at
            ' whatever XY it's already at (send MOVE_PIPETTE/_U first). No
            ' motor aspirate/dispense. For teaching: visually confirm the
            ' tip actually reaches the well bottom before confirming a point.
            On 4
            Call WaitPipetteDown
            Print #201, "OK PIPETTE_DOWN"

        ElseIf Left$(cmd$, 10) = "PIPETTE_UP" Then
            ' PIPETTE_UP — manual retraction, symmetric with PIPETTE_DOWN.
            Off 4
            Call WaitPipetteUp
            Print #201, "OK PIPETTE_UP"

        ElseIf Left$(cmd$, 5) = "PICK " Then
            ' PICK x,y — pipette (Tool 2) to (x,y), holds current U, aspirates.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            cur_u = CU(Here)
            Go XY(rx, ry, -47.700, cur_u)
            Move XY(rx, ry, -58.660, cur_u)
            On 4
            Call WaitPipetteDown
            ' Aspirate STARTS here, before the Z-descent — suction begins
            ' while the tip is still approaching, instead of waiting until
            ' full contact. Operator report 2026-08-19 (DEV-16): the tip's
            ' weight/contact was sometimes pushing/disturbing the specimen
            ' before it could be captured; starting suction early reduces
            ' reliance on physical contact. Motor runs autonomously over its
            ' own socket (#202) — StartMotorAspire only sends the command
            ' and returns immediately, it does not block the Z-descent move
            ' below. WaitMotorComplete (after that move) confirms the
            ' aspirate stroke actually finished before anything retracts.
            Call StartMotorAspire
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            Move XY(rx, ry, -62.000, cur_u)
            SpeedS 100
            AccelS 100, 100
            Call WaitMotorComplete
            Move XY(rx, ry, -58.660, cur_u)
            Off 4
            Call WaitPipetteUp
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Tool 1
            Print #201, "OK PICKED " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 7) = "PICK_U " Then
            ' PICK_U x,y,u — pipette (Tool 2) to (x,y) at explicit U, aspirates.
            ' Uses the SAME quadrant U as the EoA move that found this specimen.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            Move XY(rx, ry, -58.660, ru)
            On 4
            Call WaitPipetteDown
            ' Aspirate STARTS here, before the Z-descent — see the matching
            ' comment in PICK above (DEV-16). StartMotorAspire returns
            ' immediately, does not block the Move below; WaitMotorComplete
            ' after it confirms the aspirate stroke actually finished.
            Call StartMotorAspire
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            Move XY(rx, ry, -62.000, ru)
            SpeedS 100
            AccelS 100, 100
            Call WaitMotorComplete
            Move XY(rx, ry, -58.660, ru)
            Off 4
            Call WaitPipetteUp
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Tool 1
            Print #201, "OK PICKED " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 5) = "DROP " Then
            ' DROP x,y — pipette (Tool 2) to (x,y), holds current U, dispenses.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            sp2 = InStr(cmd$, ",")
            rx = Val(Mid$(cmd$, sp1 + 1, sp2 - sp1 - 1))
            ry = Val(Mid$(cmd$, sp2 + 1))
            cur_u = CU(Here)
            Go XY(rx, ry, -47.700, cur_u)
            ' Safety pause: arrived above the drop XY at travel height, not yet
            ' descended. Gives the operator time to hit E-STOP if this is the
            ' wrong location, before the pipette tip goes anywhere near the plate.
            Wait 3
            Move XY(rx, ry, -58.660, cur_u)
            On 4
            Call WaitPipetteDown
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            Move XY(rx, ry, -62.000, cur_u)
            SpeedS 100
            AccelS 100, 100
            Call MotorDrop
            Move XY(rx, ry, -58.660, cur_u)
            Off 4
            Call WaitPipetteUp
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Tool 1
            Print #201, "OK DROPPED " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 7) = "DROP_U " Then
            ' DROP_U x,y,u — pipette (Tool 2) to (x,y) at explicit U, dispenses.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            ' Safety pause: arrived above the drop XY at travel height, not yet
            ' descended. Gives the operator time to hit E-STOP if this is the
            ' wrong location, before the pipette tip goes anywhere near the plate.
            Wait 3
            Move XY(rx, ry, -58.660, ru)
            On 4
            Call WaitPipetteDown
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            Move XY(rx, ry, -62.000, ru)
            SpeedS 100
            AccelS 100, 100
            Call MotorDrop
            Move XY(rx, ry, -58.660, ru)
            Off 4
            Call WaitPipetteUp
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Tool 1
            Print #201, "OK DROPPED " + Str$(rx) + "," + Str$(ry)

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
    ' Safety: always retract pipette on disconnect, and close the motor
    ' socket if a pick was interrupted mid-sequence.
    Off 4
    CloseNet #201
    CloseNet #202
    GoTo server_start

    Motor Off

Fend


' =============================================================================
' EnsurePipetteUp — block until Input 20 confirms pipette is fully retracted.
' Call before any XY travel so the pipette is never dragged sideways.
' =============================================================================
Function EnsurePipetteUp
    If Sw(20) = 1 Then
        ' Already up — nothing to do
    Else
        Do
            If Sw(20) = 1 Then Exit Do
            Wait 0.05
        Loop
    EndIf
Fend


' =============================================================================
' WaitPipetteDown — block until Input 22 confirms full extension, before
' commanding the motor to aspirate.
' =============================================================================
Function WaitPipetteDown
    Do
        If Sw(22) = 1 Then Exit Do
        Wait 0.05
    Loop
Fend


' =============================================================================
' WaitPipetteUp — block until Input 20 confirms full retraction, before
' returning OK or moving the robot.
' =============================================================================
Function WaitPipetteUp
    Do
        If Sw(20) = 1 Then Exit Do
        Wait 0.05
    Loop
Fend


' =============================================================================
' MotorAspire — connect to the Novanta linear actuator, send EX G2 (draw
' back / aspirate), poll for ">" (move complete) or "?" (error), disconnect.
' No longer called by PICK/PICK_U as of 2026-08-19 (DEV-16) — they now use
' the split StartMotorAspire/WaitMotorComplete below so aspiration can
' start before the Z-descent instead of only after it. Kept here, unused,
' in case a simple synchronous aspirate is needed elsewhere later.
' =============================================================================
Function MotorAspire
    String resp$
    SetNet #202, "172.16.9.60", 503, CR, NONE, 0
    OpenNet #202 As Client
    WaitNet #202
    Print #202, "EX G2"
    Do
        If ChkNet(202) > 0 Then
            Input #202, resp$
            If InStr(resp$, ">") > 0 Then
                Exit Do
            EndIf
            If InStr(resp$, "?") > 0 Then
                Print "MotorAspire: [ERROR] motor returned error prompt."
                Exit Do
            EndIf
        EndIf
        Wait 0.1
    Loop
    CloseNet #202
Fend


' =============================================================================
' StartMotorAspire — connect to the Novanta linear actuator and send EX G2
' (draw back / aspirate), then RETURN IMMEDIATELY without waiting for the
' move to complete. The motor executes the aspirate stroke autonomously
' over its own socket — nothing here blocks the robot's own motion. Caller
' must call WaitMotorComplete afterward (typically after doing other work,
' like the robot's Z-descent move) before relying on aspiration having
' actually finished. Added 2026-08-19 (DEV-16) so aspiration can start
' before/during the descent instead of only after it — see PICK/PICK_U.
' =============================================================================
Function StartMotorAspire
    SetNet #202, "172.16.9.60", 503, CR, NONE, 0
    OpenNet #202 As Client
    WaitNet #202
    Print #202, "EX G2"
Fend


' =============================================================================
' WaitMotorComplete — poll for ">" (move complete) or "?" (error) on the
' socket opened by StartMotorAspire, then close it. Symmetric with the wait
' loop inside MotorAspire/MotorDrop, just split out so it can run AFTER
' other work (e.g. the robot's own Z move) instead of immediately.
' =============================================================================
Function WaitMotorComplete
    String resp$
    Do
        If ChkNet(202) > 0 Then
            Input #202, resp$
            If InStr(resp$, ">") > 0 Then
                Exit Do
            EndIf
            If InStr(resp$, "?") > 0 Then
                Print "WaitMotorComplete: [ERROR] motor returned error prompt."
                Exit Do
            EndIf
        EndIf
        Wait 0.1
    Loop
    CloseNet #202
Fend


' =============================================================================
' MotorDrop — connect to the Novanta linear actuator, send EX G1 (push
' forward / drop), poll for ">" (move complete) or "?" (error), disconnect.
' Ported and available, but not yet called from any command — well-travel
' and dispense are future work.
' =============================================================================
Function MotorDrop
    String resp$
    SetNet #202, "172.16.9.60", 503, CR, NONE, 0
    OpenNet #202 As Client
    WaitNet #202
    Print #202, "EX G1"
    Do
        If ChkNet(202) > 0 Then
            Input #202, resp$
            If InStr(resp$, ">") > 0 Then
                Exit Do
            EndIf
            If InStr(resp$, "?") > 0 Then
                Print "MotorDrop: [ERROR] motor returned error prompt."
                Exit Do
            EndIf
        EndIf
        Wait 0.1
    Loop
    CloseNet #202
Fend
