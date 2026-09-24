@echo off
rem SV630N 调试启动器（自动用固定路径的 Python 运行）
set PYTHONUTF8=1
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -B "%~dp0sv630n_pc.py" %*
pause
