' test_plate_station_positions.prg — standalone TCP test server for
' verifying per-station well positions and the left/right hand-config
' switch, BEFORE any of this is wired into sort_server.prg / the main
' cycle. Fully separate program, reusing device #201 (same port 2000
' sort_server.prg uses) -- safe because only one Main task ever runs on
' the controller at a time, so this and sort_server.prg can never be
' active simultaneously; no new RC+ port configuration needed. To use:
' in RC+, set THIS file's function as the project's active "Main"
' (temporarily, in place of sort_server.prg), download, run via Remote
' Ethernet Start(0) or RC+'s own Run window, same as always. Switch back
' to sort_server.prg as Main when done testing.
'
' Commands (sent from Python on port 2000, same as sort_server.prg):
'   STATUS            -> OK READY
'   GO_HOME_RIGHT      -> OK HOME_RIGHT      (Go right_home, Righty)
'   GO_HOME_LEFT       -> OK HOME_LEFT       (Go left_home, Lefty)
'   MOVE_WELL x,y,u    -> OK WELL_POSITION x,y   (Tool 2, explicit U, NO actuation)
'   DROP               -> OK DROPPED         (On 4 / confirm / Off 4 / confirm — no motor)
'   BYE
'
' Reuses EnsurePipetteUp/WaitPipetteDown/WaitPipetteUp directly from
' sort_server.prg — those Functions are already proven on real hardware,
' and every .prg file in a project shares one compiled Function namespace,
' so no need to duplicate that I/O-safety logic here.

Function Main
    test_plate_station_positions
Fend

Function test_plate_station_positions
    String cmd$
    String coord_str$
    Real rx, ry, ru
    Integer sp1, comma1, comma2

    Motor On
    Off 4
    Tool 2
    Power High
    Speed 10
    Accel 10, 10
    SpeedS 60
    AccelS 60, 60

server_start:
    OnErr GoTo client_gone
    OpenNet #201 As Server
    WaitNet #201

    Do
        OnErr GoTo client_gone

        Line Input #201, cmd$

        OnErr GoTo 0
        cmd$ = Trim$(cmd$)

        If Left$(cmd$, 6) = "STATUS" Then
            Print #201, "OK READY"

        ElseIf Left$(cmd$, 13) = "GO_HOME_RIGHT" Then
            Call EnsurePipetteUp
            Go right_home
            Print #201, "OK HOME_RIGHT"

        ElseIf Left$(cmd$, 12) = "GO_HOME_LEFT" Then
            Call EnsurePipetteUp
            Go left_home
            Print #201, "OK HOME_LEFT"

        ElseIf Left$(cmd$, 10) = "MOVE_WELL " Then
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
            Print #201, "OK WELL_POSITION " + Str$(rx) + "," + Str$(ry)

        ElseIf Left$(cmd$, 4) = "DROP" Then
            ' No motor aspirate/dispense — pure position verification,
            ' matching PIPETTE_DOWN/PIPETTE_UP's existing teaching behavior.
            On 4
            Call WaitPipetteDown
            Off 4
            Call WaitPipetteUp
            Print #201, "OK DROPPED"

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
    Off 4
    CloseNet #201
    GoTo server_start

Fend
