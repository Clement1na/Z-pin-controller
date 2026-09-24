@echo off
rem Standalone debug launcher for the SV630N indexing servo.
rem The interpreter path is not hardcoded; _python.bat probes for one.
rem Arguments are passed through, e.g.:
rem     start-servo-debug.bat 1      (pick adapter by index)
rem ASCII-only on purpose -- see the header of _python.bat.
chcp 65001 >nul
set PYTHONUTF8=1

call "%~dp0_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)

echo [Python] %ZPIN_PY%
echo.

"%ZPIN_PY%" "%~dp0..\servo\sv630n_pc.py" %*

echo.
echo Exited.
pause
