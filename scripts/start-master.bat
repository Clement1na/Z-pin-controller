@echo off
rem Launcher for the master console (the PC-side main program).
rem The interpreter path is not hardcoded; _python.bat probes for one.
rem ASCII-only on purpose -- see the header of _python.bat.
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONNOUSERSITE=1

call "%~dp0_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)

echo [Python] %ZPIN_PY%
echo.

if "%~1"=="" (
    "%ZPIN_PY%" "%~dp0..\master\master.py" --smoke-test
) else (
    "%ZPIN_PY%" "%~dp0..\master\master.py" %*
)

echo.
echo Exited.
pause
