# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包描述。入口与源码 CLI 完全同一个 `master/master.py`。

为什么需要 pathex：`master/master.py` 在运行期才 `sys.path.insert` 仓库根、
`servo/`、`cnc3d/src`，而 PyInstaller 是在**构建期**做静态分析的，运行期的
insert 它看不见。所以这三条路径必须在这里显式告诉它，否则 `sv630n_pc` /
`cnc_grbl` 这两个惰性导入会在打包时被判为"找不到"而漏掉，直到实机连伺服才炸。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把整个包解到临时目录，
Npcap 那套 EtherCAT 初始化对启动耗时敏感；而且 onedir 的 `config.toml` 就摆在
exe 旁边，操作员改端口不用解包。
"""
from pathlib import Path

# SPECPATH 是 spec 文件所在的目录（即 packaging/），仓库根是它的上一级。
ROOT = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(ROOT / "master" / "master.py")],
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
    # 交付物是命令行程序：排掉用不到的 GUI/测试栈，包体小一半且不会误带 tkinter。
    excludes=["tkinter", "unittest", "pydoc", "doctest", "pdb",
              "numpy", "matplotlib", "PIL", "pytest", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
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

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ZpinControl",
)
