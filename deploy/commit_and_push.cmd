@echo off
REM Double-click: commit the current version to Git (branch phase3) and push it to GitHub.
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0commit_changes.ps1" -Yes -Push
echo.
pause
