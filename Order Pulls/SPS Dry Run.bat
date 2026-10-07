@echo off
cd /d "%~dp0"
set "INV_PY=%~dp0..\Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"
"%RUNNER%" sps_pull.py --dry-run
echo.
pause
