# -*- coding: utf-8 -*-
"""一次后台运行的持有者：起线程、投指令、报告结束。

界面和它之间只有三个动作——`start` / `post` / 读 `running`——所以这一层不碰 Tk，
可以脱开窗口站测试。

**为什么不给状态变化挂回调**：回调会在工作线程上触发，而 Tk 不是线程安全的，
从工作线程调 `after()` 也不保证安全。所以这里不回调，界面每 50 毫秒轮询一次
`running`；只有"跑完了"这件事走队列（`queue.Queue` 本身是线程安全的）。
"""
from __future__ import annotations

import threading

from master.gui.stream import GuiCommandReader

# 运行期指令，与终端里敲的三个约定完全一致（见 master.master._execute_live_job
# 的分派循环）。界面按钮投的就是这三个。
STOP = "!"
CONTINUE = ""
FINISH_ROW = "D"


class RunController:
    def __init__(self, *, on_finished=None):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._reader: GuiCommandReader | None = None
        self._running = False
        self._on_finished = on_finished if on_finished is not None else (lambda code, error: None)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, flow) -> bool:
        """在后台线程里跑 `flow(reader) -> int`。已经在跑就返回 False，不叠加。

        每次运行都换一个新的 reader：上一次运行残留的指令绝不能漏进下一次。
        """
        with self._lock:
            if self._running:
                return False
            reader = GuiCommandReader()
            self._reader = reader
            self._running = True
            thread = threading.Thread(target=self._body, args=(flow, reader), daemon=True)
            self._thread = thread
        thread.start()
        return True

    def post(self, command: str) -> bool:
        """投一条运行期指令。没有在跑就返回 False，由调用方决定怎么提示。"""
        with self._lock:
            reader = self._reader if self._running else None
        if reader is None:
            return False
        reader.post(command)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """等这次运行结束。返回 True 表示确实结束了。"""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _body(self, flow, reader: GuiCommandReader) -> None:
        code, error = 1, None
        try:
            code = flow(reader)
        except BaseException as exc:  # noqa: BLE001 — 交给界面显示，绝不让它逃进线程栈
            error = exc
        finally:
            with self._lock:
                self._running = False
                self._reader = None
                self._thread = None
        self._on_finished(code, error)
