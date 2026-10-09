@echo off
REM Double-click to start the Spartan Dashboard (keep this window open).
cd /d "%~dp0"
python server.py %*
pause
