@echo off
chcp 65001 >nul
rem Verify process ownership and idle tasks before stopping either service.
node "%~dp0bin\code-agent-control.mjs" stop all
if errorlevel 1 pause
