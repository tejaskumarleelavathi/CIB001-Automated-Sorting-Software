' robot_server.prg -- SPEL+ TCP server running on Epson T3-B controller
' Copy this into Main.prg in RC+ 7.0
'
' Listens on TCP/IP Port 201 (port 2000, set in System Config > TCP/IP > Port 201)
' Commands: STATUS | OUT 12 ON | OUT 12 OFF | OUT 4 ON | OUT 4 OFF | BYE
' Response: "OK ..." on success
'
' NOTE: OpenNet / WaitNet / CloseNet are the correct commands for TCP/IP.
'       OpenCom is RS232C only and will give error 3336.

Function main
    String cmd$

    ' Open TCP/IP Port 201 as a server and wait for Python to connect
    OpenNet #201 As Server
    WaitNet #201

    Do
        Line Input #201, cmd$
        cmd$ = Trim$(cmd$)

        If UCase$(cmd$) = "STATUS" Then
            Print #201, "OK STATUS READY"

        ElseIf UCase$(cmd$) = "OUT 12 ON" Then
            On 12
            Print #201, "OK OUT 12 ON"

        ElseIf UCase$(cmd$) = "OUT 12 OFF" Then
            Off 12
            Print #201, "OK OUT 12 OFF"

        ElseIf UCase$(cmd$) = "OUT 4 ON" Then
            On 4
            Print #201, "OK OUT 4 ON"

        ElseIf UCase$(cmd$) = "OUT 4 OFF" Then
            Off 4
            Print #201, "OK OUT 4 OFF"

        ElseIf UCase$(cmd$) = "BYE" Then
            Print #201, "OK BYE"
            CloseNet #201
            Exit Do

        Else
            Print #201, "ERR UNKNOWN: " + cmd$
        EndIf

    Loop

Fend
