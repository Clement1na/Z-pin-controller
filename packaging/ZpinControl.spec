# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包描述。一份业务，两个入口，共用一套 `_internal/`。

| EXE | 入口 | 形态 |
|---|---|---|
| `ZpinControl.exe`   | `master/master.py`   | 控制台。终端菜单、`--smoke-test`、`--live` 等全部照旧 |
| `ZpinControlUI.exe` | `master/gui_main.py` | 窗口。双击即用，背后没有黑窗口 |

两个入口读的是**同一份** `config.toml`（`default_config_path()` 在冻结态取
`sys.executable` 的同级目录，两个 exe 都在 `dist/ZpinControl/` 下）。

为什么需要 pathex：`master/master.py` 在运行期才 `sys.path.insert` 仓库根、
`servo/`、`cnc3d/src`，而 PyInstaller 是在**构建期**做静态分析的，运行期的
insert 它看不见。所以这三条路径必须在这里显式告诉它，否则 `sv630n_pc` /
`cnc_grbl` 这两个惰性导入会在打包时被判为"找不到"而漏掉，直到实机连伺服才炸。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把整个包解到临时目录，
Npcap 那套 EtherCAT 初始化对启动耗时敏感；而且 onedir 的 `config.toml` 就摆在
exe 旁边，操作员改端口不用解包。
"""
import os
import sys
from pathlib import Path

# SPECPATH 是 spec 文件所在的目录（即 packaging/），仓库根是它的上一级。
ROOT = Path(SPECPATH).resolve().parent

ENTRY_CLI = ROOT / "master" / "master.py"
ENTRY_UI = ROOT / "master" / "gui_main.py"

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
    [str(ENTRY_CLI), str(ENTRY_UI)],
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

# `analysis.scripts` 里是「runtime hooks + 两个程序脚本」三段，**不能整个传给 EXE**：
# 实测过，那样每个 EXE 会把两个入口**都跑一遍**（先跑终端主流程再跑窗口），窗口版
# 会在后台莫名其妙地跑一次 `main()`。所以按源文件路径挑，hook 两份都留。
_PROGRAM_SCRIPTS = {os.path.normcase(str(ENTRY_CLI)), os.path.normcase(str(ENTRY_UI))}


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

exe_ui = EXE(
    pyz,
    scripts_for(ENTRY_UI),
    exclude_binaries=True,
    name="ZpinControlUI",
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

coll = COLLECT(
    exe_cli,
    exe_ui,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ZpinControl",
)
