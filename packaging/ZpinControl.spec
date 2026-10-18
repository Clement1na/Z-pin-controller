# -*- mode: python ; coding: utf-8 -*-
"""Three entry points, two independent onedir deliveries.
User: ZpinControl/ZpinControlUI.exe (master/user_gui_main.py).
Debug: ZpinControlDebug/ZpinControlDebug.exe and ZpinControl.exe.
Each delivery uses its own sibling config.toml and persistent state.
Explicit pathex/hiddenimports include lazy hardware drivers. Tcl/Tk DLLs
come from the active environment to match the collected script libraries.
"""
import os
import sys
from pathlib import Path

# SPECPATH 是 spec 文件所在的目录（即 packaging/），仓库根是它的上一级。
ROOT = Path(SPECPATH).resolve().parent

ENTRY_CLI = ROOT / "master" / "master.py"
ENTRY_UI = ROOT / "master" / "gui_main.py"
ENTRY_USER = ROOT / "master" / "user_gui_main.py"

# ---------------------------------------------------------------------------
# 让 DLL 解析先看**正在跑打包的这个环境**，而不是 PATH 上碰到的第一个同名 DLL。
#
# PyInstaller 解析 `_tkinter.pyd` 的导入表时按 PATH 找 `tcl86t.dll`。本机 PATH 上
# 排在前面的是 **base 环境**（`D:\miniconda3\Library\bin`，Tcl 8.6.15），而 tcl 的
# **脚本库** `_tcl_data\init.tcl` 是 PyInstaller 的 tkinter 钩子按 `tkinter` 自己的
# 位置收的，来自 zpin 环境（8.6.13）。两者一拼就是：
#
#     version conflict for package "Tcl": have 8.6.15, need exactly 8.6.13
#
# 结果 `tk.Tk()` 在建窗口之前就抛 TclError——而且 `gui_main.py` 那时已经把
# sys.stdout 换成了没人抽的队列流，所以**双击下去只是静默退出，什么都不显示**。
# 实测就是这个现象。
#
# 把本环境的 Library\bin 顶到 PATH 最前面，DLL 和脚本库就都来自同一个环境了。
# 不写死路径：`sys.prefix` 就是跑 PyInstaller 的那个解释器的环境根。
_ENV_BIN = Path(sys.prefix) / "Library" / "bin"
if _ENV_BIN.is_dir():
    os.environ["PATH"] = str(_ENV_BIN) + os.pathsep + os.environ.get("PATH", "")

analysis = Analysis(
    [str(ENTRY_CLI), str(ENTRY_UI), str(ENTRY_USER)],
    pathex=[str(ROOT), str(ROOT / "servo"), str(ROOT / "cnc3d" / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 三个惰性导入的模块：只在实机路径上被 import，静态分析看不到。
        "sv630n_pc",
        "cnc_grbl",
        "cnc_grbl.controller",
        "cnc_grbl.exceptions",
        "cnc_grbl.protocol",
        "pysoem",
        # pyserial 按平台条件导入具体实现，靠 importlib 而非 import 语句。
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
    ],
    hookspath=[],
    runtime_hooks=[],
    # tkinter 不能排：窗口版要用。它在共享的 _internal/ 里只占一份，控制台版
    # 用不到它也无妨。其余是测试/科学栈，两个入口都不碰。
    excludes=["unittest", "pydoc", "doctest", "pdb",
              "numpy", "matplotlib", "PIL", "pytest", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

# `analysis.scripts` 里是「runtime hooks + 三个程序脚本」三段，**不能整个传给 EXE**：
# 实测过，那样每个 EXE 会把两个入口**都跑一遍**（先跑终端主流程再跑窗口），窗口版
# 会在后台莫名其妙地跑一次 `main()`。所以按源文件路径挑，hook 三份都留。
_PROGRAM_SCRIPTS = {os.path.normcase(str(p)) for p in (ENTRY_CLI, ENTRY_UI, ENTRY_USER)}


def scripts_for(entry):
    wanted = os.path.normcase(str(entry))
    keep = []
    for name, src, typecode in analysis.scripts:
        normalized = os.path.normcase(str(src))
        if normalized in _PROGRAM_SCRIPTS and normalized != wanted:
            continue        # 那是另一个入口，不属于这个 EXE
        keep.append((name, src, typecode))
    return keep


exe_cli = EXE(
    pyz,
    scripts_for(ENTRY_CLI),
    exclude_binaries=True,
    name="ZpinControl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 必须是控制台程序：它要在终端里跑菜单、读 input()，而 `main()` 用
    # `sys.stdin.isatty()` 判断该不该给菜单——无控制台时 stdin 是 None。
    console=True,
    disable_windowed_traceback=False,
)

exe_debug = EXE(
    pyz,
    scripts_for(ENTRY_UI),
    exclude_binaries=True,
    name="ZpinControlDebug",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 必须是窗口程序：双击不该弹出黑窗口。代价是 `sys.stdout` / `sys.stderr` 是
    # None，`master/gui_main.py` 在起窗口**之前**先把它们换成队列流。
    console=False,
    # False = 未捕获异常仍然弹堆栈，而不是只显示一句"已禁用"。留着更好排查。
    disable_windowed_traceback=False,
)

exe_user = EXE(
    pyz,
    scripts_for(ENTRY_USER),
    exclude_binaries=True,
    name="ZpinControlUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

user_delivery = COLLECT(
    exe_user,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ZpinControl",
)

debug_delivery = COLLECT(
    exe_cli,
    exe_debug,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ZpinControlDebug",
)
