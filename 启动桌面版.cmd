@echo off
setlocal
cd /d "%~dp0desktop"
call npm.cmd run desktop
if errorlevel 1 pause
