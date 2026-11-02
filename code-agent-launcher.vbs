' Independent Code-agent control panel. No backend is required to open it.
Option Explicit
Dim shell, files, root, command, quote, exitCode
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
quote = Chr(34)
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & quote & root & "\scripts\open-code-agent-launcher.ps1" & quote & " -Root " & quote & root & quote
shell.CurrentDirectory = root
On Error Resume Next
exitCode = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    MsgBox "Unable to open Code-agent launcher. Run npm run build:launcher in the project directory.", vbExclamation, "Code-agent"
    WScript.Quit 1
End If
On Error GoTo 0
If exitCode <> 0 Then
    MsgBox "Unable to open Code-agent launcher. Check Code-agent Launcher.exe and run npm run build:launcher if it is missing.", vbExclamation, "Code-agent"
    WScript.Quit exitCode
End If
