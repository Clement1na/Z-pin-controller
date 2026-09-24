@echo off
rem ============================================================
rem  Resolve a Python interpreter and export it as ZPIN_PY.
rem
rem  Usage in a caller script:
rem      call "%~dp0_python.bat"
rem      if errorlevel 1 exit /b 1
rem      "%ZPIN_PY%" "...\xxx.py"
rem
rem  Two rules this file depends on:
rem    1. It MUST be pulled in with `call`. Executing it directly
rem       loses %ZPIN_PY% when the child process ends.
rem    2. No `setlocal` here -- it would scope the variable away
rem       from the caller, same problem.
rem
rem  NOTE: this file is deliberately ASCII-only. cmd.exe parses .bat
rem  bytes through the active console codepage, so non-ASCII text in
rem  `rem` lines can be torn apart and run as commands. All Chinese
rem  documentation lives in ../ENVIRONMENT.md instead.
rem
rem  Resolution order (first hit wins):
rem    0. an already-activated conda environment
rem    1. the project venv at cnc3d\.venv or .venv
rem    2. py launcher, 3.13
rem    3. py launcher, any python3
rem    4. `python` on PATH
rem ============================================================

set "ZPIN_PY="
set "_ZPIN_HERE=%~dp0"

rem ---------- 0) activated conda env (user chose it, so respect it) ----------
if defined CONDA_PREFIX (
    if exist "%CONDA_PREFIX%\python.exe" set "ZPIN_PY=%CONDA_PREFIX%\python.exe"
)
if /I "%CONDA_DEFAULT_ENV%"=="base" (
    if exist "%CONDA_PREFIX%\envs\zpin\python.exe" set "ZPIN_PY=%CONDA_PREFIX%\envs\zpin\python.exe"
)

rem ---------- 1) project venv ----------
if not defined ZPIN_PY (
    if exist "%_ZPIN_HERE%..\cnc3d\.venv\Scripts\python.exe" set "ZPIN_PY=%_ZPIN_HERE%..\cnc3d\.venv\Scripts\python.exe"
)
if not defined ZPIN_PY (
    if exist "%_ZPIN_HERE%..\.venv\Scripts\python.exe" set "ZPIN_PY=%_ZPIN_HERE%..\.venv\Scripts\python.exe"
)

rem ---------- 2) py launcher: 3.13 (the only version pysoem builds for) ----------
if not defined ZPIN_PY (
    for /f "usebackq delims=" %%p in (`py -3.13 -c "import sys;print(sys.executable)" 2^>nul`) do set "ZPIN_PY=%%p"
)

rem ---------- 3) py launcher: any python3 ----------
if not defined ZPIN_PY (
    for /f "usebackq delims=" %%p in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do set "ZPIN_PY=%%p"
)

rem ---------- 4) python on PATH ----------
if not defined ZPIN_PY (
    for /f "usebackq delims=" %%p in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "ZPIN_PY=%%p"
)

if not defined ZPIN_PY (
    echo.
    echo [ERROR] No usable Python interpreter found on this machine.
    echo.
    echo         Follow ENVIRONMENT.md to set one up first:
    echo             conda env create -f environment.yml
    echo             conda activate zpin
    echo         Python must be 3.13 -- the pysoem Windows wheel is
    echo         cp313 only. See ENVIRONMENT.md for details.
    echo.
    exit /b 1
)

rem ---------- warn (do not block) when the version is not 3.13 ----------
rem Judged by exit code rather than by capturing output: a `for /f`
rem command whose first token is a quoted path gets mis-parsed by cmd,
rem which silently yields an empty string.
"%ZPIN_PY%" -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [WARN] Selected Python is not 3.13:
    echo            %ZPIN_PY%
    echo        The servo link pysoem cannot install its dependency on any
    echo        other version. See ENVIRONMENT.md.
    echo.
)

set "_ZPIN_HERE="
exit /b 0
