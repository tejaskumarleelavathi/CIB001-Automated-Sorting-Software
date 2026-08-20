' pipette_offset_test_server.prg — EoA vs Pipette tool offset sign test
'
' Two Tool definitions:
'   Tool 1 = EoA camera offset (PROVEN 2026-08-05, cx=17.1876, cy=-77.0233)
'   Tool 2 = Pipette CANDIDATE offset — from David's CAD measurement:
'            offset between tools is nominally 40mm and 0.5mm.
'            0.5mm is along the "distance from flange" axis (matches cy,
'            already the dominant/outward-extension dimension) — pipette is
'            farther from flange, so cy = camera_cy - 0.5 (more negative,
'            same direction the arm already extends).
'            40mm is therefore along cx (lateral axis) — SIGN UNKNOWN.
'            Tried "positive X" (cx = camera_cx + 40 = 57.1876) first — wrong.
'            Now testing NEGATIVE X: cx = camera_cx - 40 = -22.8124.
'
' Commands (sent from Python on port 2000):
'   STATUS               -> OK STATUS READY
'   HOME                 -> OK HOME            (retracts pipette, lifts, goes to P0)
'   MOVE_EOA x,y,u       -> OK EOA_POSITION x,y      (Tool 1, camera to (x,y) at U=u)
'   MOVE_PIPETTE x,y,u   -> OK PIPETTE_POSITION x,y  (Tool 2, pipette to (x,y) at U=u,
'                            extends down — Output 4 ON — so you can see where it lands)
'   PIPETTE_UP           -> OK PIPETTE_UP       (retracts pipette, lifts to safe Z)
'   RING_ON / RING_OFF
'   BYE
'
' Z is never changed beyond the fixed safe/imaging heights. Speed locked low for safety.

Function Main
    pipette_offset_test
Fend

Function pipette_offset_test
    String cmd$
    String coord_str$
    Real rx, ry, ru
    Integer comma1, comma2

    Motor On
    TLSet 1, XY(17.1876, -77.0233, 0, 0)    ' EoA camera — proven
    TLSet 2, XY(-22.8124, -77.5233, 0, 0)   ' Pipette candidate — camera + (-40, -0.5)
    Power Low

    OpenNet #201 As Server
    Print "Pipette offset test server waiting on port 2000..."
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

        ' ── HOME ──────────────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "HOME" Then
            Off 4
            Tool 1
            If CZ(Here) < -47.700 Then
                Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            EndIf
            Go P0
            Print #201, "OK HOME"

        ' ── MOVE_EOA x,y,u ───────────────────────────────────────────────────
        ElseIf Left$(UCase$(cmd$), 9) = "MOVE_EOA " Then
            Off 4                                    ' retract before traveling
            Tool 1
            coord_str$ = Trim$(Mid$(cmd$, 9))
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            Move XY(rx, ry, -58.660, ru)
            Print #201, "OK EOA_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ' ── MOVE_PIPETTE x,y,u ───────────────────────────────────────────────
        ElseIf Left$(UCase$(cmd$), 13) = "MOVE_PIPETTE " Then
            Off 4                                    ' retract before traveling
            Tool 2
            coord_str$ = Trim$(Mid$(cmd$, 13))
            comma1 = InStr(coord_str$, ",")
            comma2 = InStr(Mid$(coord_str$, comma1 + 1), ",") + comma1
            rx = Val(Left$(coord_str$, comma1 - 1))
            ry = Val(Mid$(coord_str$, comma1 + 1, comma2 - comma1 - 1))
            ru = Val(Mid$(coord_str$, comma2 + 1))
            Go XY(rx, ry, -47.700, ru)
            Move XY(rx, ry, -58.660, ru)
            On 4                                     ' extend down — visual check
            Print #201, "OK PIPETTE_POSITION " + Str$(CX(Here)) + "," + Str$(CY(Here))

        ' ── PIPETTE_UP ───────────────────────────────────────────────────────
        ElseIf UCase$(cmd$) = "PIPETTE_UP" Then
            Off 4
            Move XY(CX(Here), CY(Here), -47.700, CU(Here))
            Print #201, "OK PIPETTE_UP"

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
            Off 4
            Print #201, "OK BYE"
            CloseNet #201
            Exit Do

        Else
            Print #201, "ERR UNKNOWN: " + cmd$

        EndIf

    Loop

Fend
