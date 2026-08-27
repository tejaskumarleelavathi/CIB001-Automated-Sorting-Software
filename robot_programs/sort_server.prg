' sort_server.prg — production robot TCP server
'
' Commands (sent from Python on port 2000):
'   STATUS               -> OK READY SORT_SERVER_VER=<date-tag>  (version tag
'                                                  lets a redownload be verified
'                                                  on the spot instead of trusted
'                                                  blind -- see cib/drivers/robot/driver.py's
'                                                  "robot.connected" log line,
'                                                  which surfaces this automatically
'                                                  on every connect)
'   HOME                 -> OK HOME              (ensures pipette up, lifts, always ends
'                                                  Righty at P0/right home -- if currently
'                                                  Lefty, settles at left_home first, then
'                                                  one continuous Go crosses to P0. Requires
'                                                  left_home/right_home taught points in
'                                                  this same RC+ project -- see
'                                                  camera_calibration/calibration_v3_tooloffset_server.prg
'                                                  and robot_programs/test_plate_station_positions.prg,
'                                                  both already using them.)
'   GO_HOME_LEFT          -> OK GO_HOME_LEFT       (mirrors HOME, but always ends Lefty
'                                                  at left_home -- if currently Righty,
'                                                  settles at P0/right_home first, then
'                                                  one continuous Go crosses to left_home.)
'   MOVE_IMAGE x,y       -> OK IMAGE_POSITION x,y      (Tool 1/camera, holds current U/hand)
'   MOVE_IMAGE_U x,y,u,hand -> OK IMAGE_POSITION x,y   (Tool 1/camera, explicit U + hand --
'                                                  quadrant selection. hand is "L" or "R",
'                                                  breaking change from the old 3-field
'                                                  x,y,u form -- 2026-08-26.)
'   MOVE_PIPETTE x,y     -> OK PIPETTE_POSITION x,y    (Tool 2/pipette, holds current U, NO actuation — teaching)
'   MOVE_PIPETTE_U x,y,u -> OK PIPETTE_POSITION x,y    (Tool 2/pipette, explicit U, NO actuation — teaching)
'   PIPETTE_DOWN          -> OK PIPETTE_DOWN            (extend pipette at current XY, no motor — teaching)
'   PIPETTE_UP            -> OK PIPETTE_UP              (retract pipette, no motor — teaching)
'   PICK x,y             -> OK PICKED x,y              (Tool 2/pipette, holds current U, aspirates)
'   PICK_U x,y,u,hand     -> OK PICKED x,y              (Tool 2/pipette, explicit U + hand,
'                                                  aspirates. Breaking change, same as
'                                                  MOVE_IMAGE_U above -- 2026-08-26.)
'   DROP x,y             -> OK DROPPED x,y             (Tool 2/pipette, holds current U, dispenses)
'   DROP_U x,y,u,hand     -> OK DROPPED x,y             (Tool 2/pipette, explicit U + hand,
'                                                  dispenses. Caller must always pass the
'                                                  current side's HOME U here, never a
'                                                  quadrant-pick U -- hard safety rule, see
'                                                  cib/core/orchestrator.py. Breaking
'                                                  change, same as MOVE_IMAGE_U -- 2026-08-26.)
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
' final approach. Both PICK/PICK_U and DROP/DROP_U sequence steps 1-4 the
' same way:
'   1. On 4 (pipette extends — its own now-limited, short stroke)
'   2. WaitPipetteDown (Input 22 confirms full pneumatic extension)
'   3. Move Z only, same XY, down toward the aspirate/dispense depth
'      (robot/EOA controlled descent — this is what actually reaches the
'      specimen/well). Runs at SpeedS 10 / AccelS 10,10 (mm/sec — Move is a
'      linear/CP command, controlled by SpeedS/AccelS, NOT Speed/Accel,
'      which only affect Go/PTP commands and do nothing to Move. An
'      earlier attempt at slowing this down used Speed/Accel and had no
'      effect — found and fixed 2026-08-19.) The whole point of this being
'      a robot-controlled move instead of the pneumatic is a controlled
'      approach, so it must run slow. SpeedS/AccelS are restored to the
'      fast baseline (100 / 100,100) once the FULL depth is reached — that
'      same fast setting then carries through whichever retract-up move
'      happens first (step 5) and the final travel move (step 7).
'      DROP/DROP_U: one move straight to -60.000 (2026-08-21 — less
'      descent needed to reach the well than PICK; was -62.000 like PICK
'      before this, operator's call), THEN step 4 (MotorDrop) fires.
'      PICK/PICK_U: split into two moves with step 4 sandwiched between
'      them (2026-08-21, operator's call, current behavior — history
'      below): first to -60.000, THEN StartMotorAspire (step 4, does not
'      block), THEN continue to -62.000 while it runs, THEN
'      WaitMotorComplete confirms it actually finished before anything
'      retracts.
'   4. Aspirate (PICK) or dispense (DROP). DROP: synchronous MotorDrop,
'      fires after step 3 completes. PICK: StartMotorAspire, fires
'      partway through step 3 (at -60.000, continuing to -62.000 while it
'      runs) — see step 3 above. This has moved three times on 2026-08-19/
'      21 while tuning around one problem (tip contact pushing/disturbing
'      the specimen before capture): DEV-16 (2026-08-19) started it BEFORE
'      any descent; a same-day-2026-08-21 revert moved it to AFTER full
'      descent (matching DROP); the current mid-descent trigger (also
'      2026-08-21) is a middle ground between those two. StartMotorAspire/
'      WaitMotorComplete (the non-blocking split) are back in use by PICK/
'      PICK_U; MotorAspire (synchronous) is unused again for now.
' Steps 5-6 (the retract order) DIFFER between PICK and DROP as of
' 2026-08-21:
'   DROP/DROP_U — unchanged, robot/EOA retracts FIRST (fast, same as step 3),
'     THEN Off 4 (pipette retracts second, only once EOA is already clear).
'   PICK/PICK_U — REVERSED: Off 4 (pipette retracts FIRST) THEN robot/EOA Z
'     retracts. Operator's own hardware observation: the aspirated specimen
'     was falling back into the liquid during retraction under the old
'     order. The pneumatic's short stroke clears the tip of the water
'     surface almost immediately; doing that before the slower, longer
'     robot Z travel means the tip isn't dragged through/near the surface
'     for the whole climb. Safe regardless of order — the mount-raise above
'     means the pneumatic alone can't reach the dish/well bottom at ANY
'     robot Z, so retracting it first at -62.000 introduces no new
'     collision risk.
'   7. WaitPipetteUp, then Move to -47.700 travel height (only XY travel move
'      happens after the pipette is confirmed fully up)
' Steps 3 and 5 are Z-only (no XY change) so they never violate "no XY travel
' with the pipette down" — only step 7's travel move does, and it only runs
' after WaitPipetteUp confirms the pipette is clear.
'
' CP (continuous path) blending, pipette-UP moves only (2026-08-22, operator
' request — "add blending... when pipette is up only"): PICK/PICK_U's step 7
' lift (the two Z-only moves from -62.000/-60.000 up to -47.700) runs inside
' CP On / CP Off. CP is matched with CP Off in the SAME command handler
' before it returns — CP is not auto-reset between TCP commands (only at
' controller startup/motor-on/task end), so leaving it set would silently
' affect whatever command runs next.
' Also tried this on DROP/DROP_U's initial travel-to + approach-to -58.660,
' converting the travel move from Go (PTP) to Move (CP) so it could blend
' with the descent. REVERTED same day — real hardware showed robot_drop got
' SLOWER (~6.2-6.4s baseline -> 8.5s): Move is capped by SpeedS (a straight-
' line mm/sec limit), while Go uses Speed (% of max joint speed) — for this
' SCARA's large pick-to-drop lateral hop, PTP's per-joint profile beat the
' 120mm/s linear cap by more than the blended corner saved. DROP/DROP_U's
' travel move is back to Go, unblended.
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
    String hand$
    Real rx, ry, cur_u, ru
    Integer sp1, sp2
    Integer comma1, comma2, comma3

    Motor On
    ' CP does NOT auto-reset except at controller startup/motor-on/task end,
    ' so it can persist from whatever ran before this task -- and CP
    ' blending cannot cross a Righty<->Lefty hand change (error 4037),
    ' which HOME/GO_HOME_LEFT now need to do. Defensive: clears it
    ' regardless of what left it on (2026-08-26 real-hardware finding,
    ' ported from camera_calibration/calibration_v3_tooloffset_server.prg).
    CP Off
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
    ' Speed/Accel govern every Go (PTP) move in this whole program — HOME,
    ' MOVE_IMAGE(_U), and PICK/PICK_U/DROP/DROP_U's initial travel-to move
    ' all use it, since it's a global interpreter setting, set once, never
    ' touched elsewhere. Raised from 5/5,5 (5% of max — extremely
    ' conservative, likely never revisited since early bring-up) to 20/
    ' 20,20 as a first optimize-speed step (2026-08-21), then to 25/25,25
    ' (2026-08-22, operator request — "increase the robot speed just a
    ' bit"): another modest step, not the max — watch the first few real
    ' cycles closely (E-stop ready) before pushing higher. This does NOT
    ' touch the controlled final descent (SpeedS 10/AccelS 10,10, set
    ' separately inside PICK/DROP).
    Speed 25
    Accel 25, 25
    ' SpeedS/AccelS govern Move (linear/CP) commands — Speed/Accel above only
    ' govern Go (PTP) commands and have NO effect on Move. Every fine-motion
    ' command in this program uses Move, so this is the real "fast/slow"
    ' knob. Set once here as the default "fast" baseline; PICK/PICK_U/
    ' DROP/DROP_U temporarily drop to a slow SpeedS/AccelS for the
    ' controlled final descent only, then restore these same fast values.
    ' Raised 100 -> 120 mm/sec (2026-08-22, operator request, same "just a
    ' bit" ask as above) — also now the speed for DROP/DROP_U's travel-to
    ' move, converted from Go to Move below to enable CP blending into the
    ' approach descent.
    SpeedS 120
    AccelS 120, 120

server_start:
    OpenNet #201 As Server
    WaitNet #201

    Do
        OnErr GoTo client_gone

        Line Input #201, cmd$

        OnErr GoTo 0
        cmd$ = Trim$(cmd$)
        ' Defensive CP Off before EVERY command, not just once at program
        ' startup (2026-08-27 real-hardware finding). The one-time CP Off
        ' near the top of this Function only runs once when the task first
        ' starts -- a client disconnect/BYE only jumps back to server_start:
        ' (never re-runs that setup), so if CP was ever left ON by an
        ' interrupted command from earlier in this task's lifetime (this
        ' session or before), it silently persists across every reconnect.
        ' Confirmed on hardware: a stale CP On caused GO_HOME_LEFT's two
        ' discrete Go P0 / Go left_home crossing moves to get blended into
        ' one continuous CP path instead -- fast, and landing in the wrong
        ' posture instead of cleanly at left_home -- then the very next
        ' command's plain Move hit the same bad state and threw error 4037.
        ' Cheap no-op if CP is already off; any command that WANTS CP on
        ' (PICK/PICK_U's blended retract) still explicitly turns it on for
        ' just that block and off again right after, unaffected by this.
        CP Off

        If Left$(cmd$, 6) = "STATUS" Then
            ' Version tag appended so a redownload can be verified on the
            ' spot (send STATUS, read the tag) instead of trusting that a
            ' download-over-a-running-task actually replaced the code --
            ' this exact class of mixup has cost real debugging time this
            ' session more than once. BUMP THIS every time sort_server.prg
            ' changes.
            Print #201, "OK READY SORT_SERVER_VER=2026-08-27e"

        ElseIf Left$(cmd$, 4) = "HOME" Then
            ' Always ends Righty at P0 (right home). Real-hardware findings
            ' (2026-08-25/26, proven in calibration_v3_tooloffset_server.prg
            ' and ported here): (1) Move has no Hand parameter at all -- a
            ' bare Move here threw error 4037 when the current hand was
            ' Lefty; (2) a bare Go XY(...) with no hand does NOT reliably
            ' preserve the arm's current hand -- it can silently default
            ' toward Righty, so any Z-lift here must be explicitly /L or /R
            ' to match whatever hand is ACTUALLY current; (3) crossing hands
            ' must be a single bare Go with nothing in between -- a Z-lift
            ' step with the opposite hand's qualifier right before the
            ' crossing move reorients the arm IN PLACE at the wrong home
            ' instead of sweeping across in one continuous motion. So: if
            ' currently Lefty, first settle at left_home (same-hand, safe),
            ' THEN one bare Go P0 to cross. If already Righty, just the
            ' same-hand return.
            ' Slowed to Speed 5/Accel 5,5 for the whole homing sequence
            ' (operator feedback, 2026-08-26 -- full-speed 25/25,25 was too
            ' fast for this move, especially the hand-crossing sweep). Same
            ' value already proven safe for this exact right_home<->left_home
            ' transition in test_left_right_home.prg. Restored to the normal
            ' 25/25,25 baseline before returning so PICK/DROP/MOVE_IMAGE etc.
            ' aren't left running slow afterward.
            Call EnsurePipetteUp
            Tool 1
            Speed 5
            Accel 5, 5
            If Hand = Lefty Then
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /L
                EndIf
                Go left_home
                Go P0
            Else
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /R
                EndIf
                Go P0
            EndIf
            Speed 25
            Accel 25, 25
            Print #201, "OK HOME"

        ElseIf Left$(cmd$, 12) = "GO_HOME_LEFT" Then
            ' Mirrors HOME above, but the target is left_home/Lefty instead
            ' of P0/Righty. Same findings apply, mirrored: Go not Move
            ' (Move has no Hand parameter), explicit /L or /R on any Z-lift
            ' instead of assuming a bare Go preserves current hand, and a
            ' single bare Go with nothing in between for the actual
            ' crossing. Slowed the same way as HOME (operator feedback).
            Call EnsurePipetteUp
            Tool 1
            Speed 5
            Accel 5, 5
            If Hand = Righty Then
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /R
                EndIf
                Go P0
                Go left_home
            Else
                If CZ(Here) < -47.700 Then
                    Go XY(CX(Here), CY(Here), -47.700, CU(Here)) /L
                EndIf
                Go left_home
            EndIf
            Speed 25
            Accel 25, 25
            Print #201, "OK GO_HOME_LEFT"

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
            ' MOVE_IMAGE_U x,y,u,hand — same as MOVE_IMAGE but with an
            ' explicit U angle AND hand (2026-08-26, multi-station
            ' integration) instead of holding whatever U/hand the arm
            ' currently has. Used for quadrant-based U selection (see
            ' cib/core/orchestrator.py).
            Tool 1
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            comma3 = InStr(Mid$(coord_str$, comma2 + 1), ",") + comma2
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1, comma3 - comma2 - 1))
            hand$ = Mid$(coord_str$, comma3 + 1)
            ' Left$(hand$,1) not hand$= -- immune to trailing terminator
            ' garbage, same reasoning as test_plate_station_positions.prg.
            If Left$(hand$, 1) = "L" Then
                Go XY(rx, ry, -47.700, ru) /L
            Else
                Go XY(rx, ry, -47.700, ru) /R
            EndIf
            ' Real-hardware finding (2026-08-27): a bare Move XY(...) has
            ' no Hand parameter at all, and its own internal target-hand
            ' resolution silently defaults toward Righty regardless of the
            ' arm's actual current hand -- fine for Righty (matches the
            ' default), but throws error 4037 ("point attribute...differ")
            ' for Lefty, since the resolved target disagrees with the
            ' Lefty position the qualified Go above just established.
            ' Fix: assign the target to a point variable and explicitly set
            ' ITS Hand attribute via the Hand statement before Move'ing to
            ' it (P1 = coords / Hand P1, Lefty|Righty / Move P1 -- confirmed
            ' valid via the SPEL+ Language Reference's own Hand Statement
            ' example) instead of a bare XY(...) expression that carries no
            ' hand attribute of its own. P100 is an unused scratch point
            ' number (P0 = right_home, P1 used elsewhere in this project).
            P100 = XY(rx, ry, -58.660, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
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
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            Move XY(rx, ry, -60.000, cur_u)
            ' Aspirate triggers HERE, partway down (-60.000) — not before any
            ' descent (DEV-16, 2026-08-19) and not only after reaching full
            ' depth (2026-08-21's first revert) but a middle ground, same
            ' day: give the tip a couple mm of real approach first (unlike
            ' DEV-16's from-the-top start), then start suction WHILE still
            ' closing the last 2mm to -62.000 (unlike waiting for full
            ' depth). StartMotorAspire returns immediately — the Move below
            ' to -62.000 is NOT blocked by it. WaitMotorComplete (after
            ' reaching -62.000) confirms the aspirate stroke actually
            ' finished before anything retracts.
            Call StartMotorAspire
            Move XY(rx, ry, -62.000, cur_u)
            SpeedS 120
            AccelS 120, 120
            Call WaitMotorComplete
            ' Pipette (pneumatic) retracts FIRST, THEN robot/EOA Z — reversed
            ' 2026-08-21 from the original EOA-first order (operator's own
            ' hardware observation: the aspirated specimen was falling back
            ' into the liquid during retraction). Off 4 is a short stroke
            ' that pulls the tip up close to the mount almost immediately;
            ' doing that before the slower, longer robot Z travel clears the
            ' tip of the water surface sooner, instead of dragging a still-
            ' extended tip through/near the surface during the whole Z climb.
            ' Safe regardless of order: the mount-raise in DEV-11 means the
            ' pneumatic alone can't reach the dish/well bottom at ANY robot Z,
            ' so retracting it first at -62.000 introduces no new collision
            ' risk — same reasoning as it already being safe extended at
            ' travel height.
            Off 4
            Call WaitPipetteUp
            ' Blended (CP On) — 2026-08-22, operator request: only while the
            ' pipette is UP, never during the controlled descent above. CP
            ' Off before Tool 1/Print so the setting never leaks into
            ' whatever command runs next (CP is NOT auto-reset between TCP
            ' commands — only at controller startup/motor-on/task end, per
            ' Epson SPEL+ Language Reference). Rewrote the final lift to
            ' explicit XY/U instead of CX(Here)/CY(Here)/CU(Here) — motion
            ' commands referencing Here bypass CP path synthesis entirely
            ' regardless of CP state, so the old Here-based move could never
            ' have blended anyway.
            CP On
            Move XY(rx, ry, -58.660, cur_u)
            Move XY(rx, ry, -47.700, cur_u)
            CP Off
            Tool 1
            Print #201, "OK PICKED " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 7) = "PICK_U " Then
            ' PICK_U x,y,u,hand — pipette (Tool 2) to (x,y) at explicit U
            ' AND hand (2026-08-26, multi-station integration), aspirates.
            ' Uses the SAME quadrant U/hand as the EoA move that found this
            ' specimen.
            ' Real-hardware finding (2026-08-27): every bare Move XY(...)
            ' below has no Hand parameter and its own internal target-hand
            ' resolution silently defaults toward Righty regardless of the
            ' arm's actual current hand -- fine for Righty, but throws
            ' error 4037 ("point attribute...differ") for Lefty. Fixed by
            ' assigning each target to scratch point P100 and explicitly
            ' setting ITS Hand attribute via the Hand statement before
            ' Move'ing to it (confirmed valid via the SPEL+ Language
            ' Reference's own Hand Statement example), instead of a bare
            ' XY(...) expression that carries no hand attribute of its own.
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            comma3 = InStr(Mid$(coord_str$, comma2 + 1), ",") + comma2
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1, comma3 - comma2 - 1))
            hand$ = Mid$(coord_str$, comma3 + 1)
            If Left$(hand$, 1) = "L" Then
                Go XY(rx, ry, -47.700, ru) /L
            Else
                Go XY(rx, ry, -47.700, ru) /R
            EndIf
            P100 = XY(rx, ry, -58.660, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            On 4
            Call WaitPipetteDown
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            P100 = XY(rx, ry, -60.000, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            ' Aspirate triggers HERE, partway down — see the matching comment
            ' in PICK above (2026-08-21: mid-descent, not before or after).
            Call StartMotorAspire
            P100 = XY(rx, ry, -62.000, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            SpeedS 120
            AccelS 120, 120
            Call WaitMotorComplete
            ' Pipette retracts FIRST, then robot/EOA Z — see the matching
            ' comment in PICK above (2026-08-21: fixes specimen falling back
            ' into the liquid during retraction).
            Off 4
            Call WaitPipetteUp
            ' Blended (CP On) — see the matching comment in PICK above
            ' (2026-08-22: only while pipette is UP, CP Off before Print so
            ' it never leaks into the next command, Here-based moves
            ' rewritten to explicit XY/U since Here bypasses CP entirely).
            CP On
            P100 = XY(rx, ry, -58.660, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            P100 = XY(rx, ry, -47.700, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            CP Off
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
            ' Tried CP-blending this travel+approach 2026-08-22 by converting
            ' this move from Go (PTP) to Move (CP), so it could blend with
            ' the descent below. REVERTED same day — real hardware showed
            ' robot_drop got SLOWER (~6.2-6.4s baseline -> 8.5s), confirming
            ' the exact risk flagged when this was added: Move is capped by
            ' SpeedS (a straight-line mm/sec limit, 120), while Go uses
            ' Speed (% of max joint speed, 25) — for this SCARA's large
            ' lateral hop, PTP's per-joint profile is evidently faster than
            ' the 120mm/s linear cap, and that cost more than the blended
            ' corner saved. Back to Go — no CP wrap here (a lone Move with
            ' nothing to blend into is pointless anyway once this reverts).
            Go XY(rx, ry, -47.700, cur_u)
            ' 3s E-stop reaction pause removed 2026-08-21 (optimize-speed pass,
            ' operator's own call — "we don't need 3 secs there, remove it").
            ' Was here to give a reaction window before descending near the
            ' plate; now descends immediately after arriving at travel height.
            Move XY(rx, ry, -58.660, cur_u)
            On 4
            Call WaitPipetteDown
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            ' -60.000, not -62.000 (PICK's aspirate depth) — DROP-only, less
            ' descent needed to reach the well (2026-08-21, operator's call).
            Move XY(rx, ry, -60.000, cur_u)
            SpeedS 120
            AccelS 120, 120
            Call MotorDrop
            Move XY(rx, ry, -58.660, cur_u)
            Off 4
            Call WaitPipetteUp
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Tool 1
            Print #201, "OK DROPPED " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 7) = "DROP_U " Then
            ' DROP_U x,y,u,hand — pipette (Tool 2) to (x,y) at explicit U
            ' AND hand (2026-08-26, multi-station integration), dispenses.
            ' Caller (orchestrator.py) is responsible for always passing
            ' the current side's HOME U here, never a quadrant-pick U --
            ' this command itself just goes wherever it's told.
            ' Real-hardware finding (2026-08-27): every bare Move XY(...)
            ' below has no Hand parameter and its own internal target-hand
            ' resolution silently defaults toward Righty regardless of the
            ' arm's actual current hand -- fine for Righty, but throws
            ' error 4037 ("point attribute...differ") for Lefty. Fixed by
            ' assigning each target to scratch point P100 and explicitly
            ' setting ITS Hand attribute via the Hand statement before
            ' Move'ing to it (same pattern as PICK_U above).
            Off 4
            Call EnsurePipetteUp
            Tool 2
            sp1 = InStr(cmd$, " ")
            coord_str$ = Mid$(cmd$, sp1 + 1)
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            comma3 = InStr(Mid$(coord_str$, comma2 + 1), ",") + comma2
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1, comma3 - comma2 - 1))
            hand$ = Mid$(coord_str$, comma3 + 1)
            ' Reverted the CP-blended Go->Move travel conversion here too —
            ' see the matching comment in DROP above (2026-08-22: real
            ' hardware showed robot_drop got slower, not faster, once this
            ' travel move was capped by SpeedS instead of using Go's PTP
            ' joint-speed profile).
            If Left$(hand$, 1) = "L" Then
                Go XY(rx, ry, -47.700, ru) /L
            Else
                Go XY(rx, ry, -47.700, ru) /R
            EndIf
            ' 3s E-stop reaction pause removed 2026-08-21 (optimize-speed pass,
            ' operator's own call — "we don't need 3 secs there, remove it").
            ' Was here to give a reaction window before descending near the
            ' plate; now descends immediately after arriving at travel height.
            P100 = XY(rx, ry, -58.660, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            On 4
            Call WaitPipetteDown
            ' Slow, controlled final approach — this is the whole point of the
            ' two-stage descent (DEV-11), so it must not run at normal travel
            ' speed. SpeedS/AccelS (not Speed/Accel — Move is a linear/CP
            ' command) restored to the fast baseline right after.
            SpeedS 10
            AccelS 10, 10
            ' -60.000, not -62.000 (PICK's aspirate depth) — DROP-only, less
            ' descent needed to reach the well (2026-08-21, operator's call).
            P100 = XY(rx, ry, -60.000, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            SpeedS 120
            AccelS 120, 120
            Call MotorDrop
            P100 = XY(rx, ry, -58.660, ru)
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
            Off 4
            Call WaitPipetteUp
            P100 = XY(CX(Here), CY(Here), -47.700, CU(Here))
            If Left$(hand$, 1) = "L" Then
                Hand P100, Lefty
            Else
                Hand P100, Righty
            EndIf
            Move P100
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
' Not currently called by PICK/PICK_U (2026-08-21) — they use the
' non-blocking StartMotorAspire/WaitMotorComplete split below instead, so
' aspiration can start partway through the descent instead of only after
' it fully completes. Kept here, unused, in case a simple synchronous
' aspirate is needed again later — see the sort_server header comment for
' the full history of this moving around.
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
' like the rest of the robot's Z-descent move) before relying on
' aspiration having actually finished. Added 2026-08-19 (DEV-16); PICK/
' PICK_U currently call this partway through the descent (at -60.000,
' continuing to -62.000 while it runs) — see the sort_server header
' comment for the full history of exactly when this fires, which has
' moved a few times.
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
