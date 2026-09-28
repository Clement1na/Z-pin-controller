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

rem No arguments -> master.py shows its mode menu. Do NOT default to
rem --smoke-test here any more: that would skip the menu, and a double-click
rem is exactly when the operator wants to choose. master.py decides for
rem itself whether stdin is a terminal; if it is not, it prints a usage
rem error instead of waiting for input.
rem Pass arguments to skip the menu, e.g.:
rem     start-master.bat --smoke-test
rem     start-master.bat --live --debug
"%ZPIN_PY%" "%~dp0..\master\master.py" %*

echo.
echo Exited.
pause
