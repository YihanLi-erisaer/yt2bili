@echo off
cd /d "%~dp0"
node desktop\scripts\package-windows.mjs
if errorlevel 1 (
  echo Build failed. Please check the error above.
  pause
  exit /b 1
)
echo Windows files are in dist\windows
pause
