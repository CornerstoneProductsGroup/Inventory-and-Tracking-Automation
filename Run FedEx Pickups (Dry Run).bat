@echo off
setlocal
cd /d "%~dp0Inventory Submissions"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo FedEx Pickups DRY RUN — fill the form (no submit) for next-business-day Ground pickups from today's Lowe's CSV
echo   Our Warehouse ^(warehouse vendors^) and Post Protector ^(address changed^)
echo   Add --location warehouse or --location postprotector to do just one.
echo.
"%RUNNER%" "run_fedex_pickup.py" --dry-run %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo FedEx pickup dry run finished with errors ^(exit %ERR%^).
) else (
  echo FedEx pickup dry run finished.
)
pause
exit /b %ERR%
