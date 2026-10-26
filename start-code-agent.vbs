' Code-agent launcher - starts missing services and opens the workbench.
' Double-click this file. Services run hidden; logs go to logs\.
' To stop: run stop-code-agent.bat.
'
' --no-open        Do not open a browser.
' --start-only     Start missing services (the default).
' --restart-backend Restart our backend only after all work is confirmed idle.
Option Explicit

Dim sh, fso, root, logDir, dispatchLog, q, cmd, i, noOpen, restartBackend, restartCode, locked
Dim backendReady, frontendReady, backendError, frontendError
noOpen = False
restartBackend = False
locked = False

For i = 0 To WScript.Arguments.Count - 1
    Select Case LCase(WScript.Arguments(i))
        Case "--no-open": noOpen = True
        Case "--start-only": restartBackend = False
        Case "--restart-backend": restartBackend = True
        Case "--internal-locked": locked = True
        Case Else: Fail "Unknown launcher option: " & WScript.Arguments(i)
    End Select
Next

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = root & "\logs"
q = Chr(34)

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

' Serialize the entire flow, including the detached service launch and probes.
' The internal call runs while its hidden PowerShell parent owns the mutex.
If Not locked Then
    If Not fso.FileExists(root & "\scripts\launch-code-agent.ps1") Then
        Fail "The launcher lock helper is missing."
    End If
    ' Never redirect the entire process tree to one shared dispatch file. A
    ' surviving service or another program may still have that file open.
    ' The wrapper records this invocation itself, with short-lived handles.
    dispatchLog = logDir & "\launcher-" & Replace(fso.GetTempName, ".tmp", ".log")
    cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & _
          q & root & "\scripts\launch-code-agent.ps1" & q & " -Root " & q & root & q & _
          " -LogPath " & q & dispatchLog & q
    If Not restartBackend Then cmd = cmd & " -StartOnly"
    If noOpen Then cmd = cmd & " -NoOpen"
    restartCode = RunHiddenAndWait(cmd)
    If restartCode = 13 Then
        Fail "Another launcher is already starting or restarting these services. Wait for it to finish."
    End If
    If restartCode = 12 Then
        FinishWithError "Backend restart deferred: work, a question, or an approval is still active." & _
             vbCrLf & "Finish the current work and run this launcher again. Services and tasks have been preserved.", 12
    End If
    If restartCode <> 0 Then
        Fail "The launcher could not complete (exit " & restartCode & ")." & _
             vbCrLf & "Check this launch: " & dispatchLog & _
             vbCrLf & "Backend restart details: " & logDir & "\launcher.log"
    End If
    WScript.Quit restartCode
End If

' Restart only our verified backend, and only after all executions are idle.
' Wait hidden for the guard/stop helper; services themselves remain detached.
If restartBackend Then
    If Not fso.FileExists(root & "\scripts\restart-code-agent-backend.ps1") Then
        Fail "The backend restart helper is missing."
    End If
    sh.CurrentDirectory = root
    cmd = "cmd /c " & q & "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & _
          q & root & "\scripts\restart-code-agent-backend.ps1" & q & " -Root " & q & root & q & _
          " >> " & q & logDir & "\launcher.log" & q & " 2>&1" & q
    restartCode = RunHiddenAndWait(cmd)
    If restartCode = 12 Then
        FinishWithError "Backend restart deferred: an execution, queued task, question, or approval is still active." & _
             vbCrLf & "Finish the current work and run this launcher again." & vbCrLf & _
             "Services and tasks have been preserved. See logs\launcher.log for status counts.", 12
    End If
    If restartCode <> 0 Then
        Fail "Backend restart could not be completed safely. No replacement backend was started." & _
             vbCrLf & "Check " & logDir & "\launcher.log"
    End If
End If

' Probe each service independently: a surviving frontend cannot recover its backend.
backendReady = IsUp("http://127.0.0.1:8765/healthz", backendError)
frontendReady = IsUp("http://127.0.0.1:5175/", frontendError)

' ---- 1. backend (hidden window, log redirected) ----
If Not backendReady Then
    If Not fso.FileExists(root & "\.venv\Scripts\python.exe") Then
        Fail "Backend Python was not found: " & root & "\.venv\Scripts\python.exe"
    End If
    sh.CurrentDirectory = root
    cmd = "cmd /c " & q & q & root & "\.venv\Scripts\python.exe" & q & _
          " -m code_agent.server --host 127.0.0.1 --port 8765 >> " & q & logDir & "\backend.log" & q & " 2>&1" & q
    StartHidden cmd, "Backend"
End If

' ---- 2. frontend (hidden window, log redirected) ----
If Not frontendReady Then
    If Not fso.FileExists(root & "\web\node_modules\.bin\vite.cmd") Then
        Fail "Frontend Vite was not found: " & root & "\web\node_modules\.bin\vite.cmd"
    End If
    sh.CurrentDirectory = root & "\web"
    cmd = "cmd /c " & q & q & root & "\web\node_modules\.bin\vite.cmd" & q & _
          " --host 127.0.0.1 --port 5175 --strictPort >> " & q & logDir & "\frontend.log" & q & " 2>&1" & q
    StartHidden cmd, "Frontend"
End If

' ---- 3. wait until both return HTTP 2xx (up to 40 checks) ----
For i = 1 To 40
    If backendReady And frontendReady Then Exit For
    WScript.Sleep 1000
    backendReady = IsUp("http://127.0.0.1:8765/healthz", backendError)
    frontendReady = IsUp("http://127.0.0.1:5175/", frontendError)
Next

' Silent on success - the browser window opening IS the feedback.
' Only surface a dialog when something actually went wrong.
If Not backendReady Or Not frontendReady Then
    Fail "Code-agent services did NOT become ready." & vbCrLf & vbCrLf & _
         "Backend: " & backendError & vbCrLf & _
         "Frontend: " & frontendError & vbCrLf & vbCrLf & _
         "Check logs:" & vbCrLf & _
         logDir & "\backend.log" & vbCrLf & _
         logDir & "\frontend.log"
End If

If Not noOpen Then sh.Run "http://127.0.0.1:5175/", 1, False
WScript.Quit 0

' ---- helpers ----
Function IsUp(url, ByRef healthError)
    Dim request, status, probeError
    IsUp = False
    healthError = ""
    On Error Resume Next
    Set request = CreateObject("WinHttp.WinHttpRequest.5.1")
    request.SetTimeouts 500, 500, 1000, 2000
    request.SetProxy 1 ' Local services must not go through a system HTTP proxy.
    request.Open "GET", url, False
    request.Option(6) = False ' Do not count a redirected page as this service being ready.
    request.Send
    probeError = Err.Description
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        healthError = "Health check failed: " & probeError
        Exit Function
    End If
    status = request.Status
    probeError = Err.Description
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        healthError = "Health check failed: " & probeError
        Exit Function
    End If
    On Error GoTo 0
    IsUp = (status >= 200 And status < 300)
    healthError = "HTTP " & status
End Function

Sub StartHidden(command, serviceName)
    Dim launchError
    On Error Resume Next
    sh.Run command, 0, False
    launchError = Err.Description
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Fail serviceName & " could not be started: " & launchError
    End If
    On Error GoTo 0
End Sub

Function RunHiddenAndWait(command)
    Dim launchError, result
    On Error Resume Next
    result = sh.Run(command, 0, True)
    launchError = Err.Description
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Fail "The backend restart check could not run: " & launchError
    End If
    On Error GoTo 0
    RunHiddenAndWait = result
End Function

Sub Fail(message)
    FinishWithError message, 1
End Sub

Sub FinishWithError(message, exitCode)
    ' Script/automation callers receive text and an exit code; double-clicks
    ' receive one visible dialog from the outer WScript launcher.
    If InStr(LCase(WScript.FullName), "cscript.exe") > 0 Then
        WScript.Echo message
    Else
        MsgBox message, vbExclamation, "Code-agent"
    End If
    WScript.Quit exitCode
End Sub
