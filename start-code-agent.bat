@echo off
rem Code-agent launcher - starts missing services with NO console window.
rem Real logic lives in start-code-agent.vbs: reuse healthy backend 8765
rem and frontend 5175, both hidden (logs go to logs\).
rem
rem Tip: for a fully window-less start, double-click start-code-agent.vbs instead
rem      (a .bat double-click always flashes a console briefly).
rem
rem --no-open : do NOT open a browser.
rem --start-only : only start missing services, preserving healthy services.
rem --restart-backend : explicitly request a guarded idle backend restart.
rem             NOTE: keep this file ASCII-only - cmd.exe reads .bat with the
rem             OEM codepage, so UTF-8 Chinese comments get parsed as commands.
chcp 65001 >nul
start "" wscript "%~dp0start-code-agent.vbs" %*
exit /b 0
