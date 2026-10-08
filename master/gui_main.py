# -*- coding: utf-8 -*-
"""图形界面入口。打包成 `ZpinControlUI.exe`（无控制台窗口）。

和 `master/master.py` 是**两个入口、一份业务**：那边继续服务终端与脚本化调用
（`--smoke-test` / `--simulate` / `--live`），这边只服务双击启动。两边共用同一个
`default_config_path()`，所以在打包产物里读的是同一个 `config.toml`。
"""
from __future__ import annotations

import argparse
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-pin 五针植入控制（图形界面）")
    parser.add_argument("--config", type=Path, default=None,
                        help="配置模板路径；默认取 exe 同级的 config.toml")
    parser.add_argument("--results-dir", type=Path, default=None,
                        help="CSV/JSON 结果目录；默认取配置同级的 runs")
    args = parser.parse_args(argv)

    from master.master import default_config_path

    # 无控制台窗口时 sys.stdout / sys.stderr 是 None，此后任何一句 print() 都会抛
    # AttributeError。所以**第一件事**就是换成队列流——设备的调试回显、总控的进度、
    # OperatorGate 的提示语全都在运行期才取 sys.stdout，换掉这一个就全都接上了。
    sink: queue.Queue = queue.Queue()
    from master.gui.stream import QueueStream
    stream = QueueStream(sink)
    sys.stdout = stream
    sys.stderr = stream

    import tkinter as tk
    from tkinter import messagebox

    from master.gui.app import MainWindow

    config_path = args.config if args.config is not None else default_config_path()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"错误：无法创建窗口：{exc}")
        return 2

    try:
        MainWindow(root, sink, config_path=config_path, results_dir=args.results_dir)
    except Exception as exc:  # noqa: BLE001 — 起窗口失败必须让人看见，不能静默退出
        messagebox.showerror("Z-pin", f"界面初始化失败：{exc}")
        print(f"错误：界面初始化失败：{exc}")
        return 2

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
