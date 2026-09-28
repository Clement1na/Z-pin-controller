# -*- coding: utf-8 -*-
"""设备回话回显：调试模式下把设备自己说的话原样打到终端。

接缝约定和仓库里其他接缝一致（`serial_factory=None`、`device_factory=None`）：
**`echo=None` 表示全静默**。只有 `master.master` 的实机路径会注入真的
`DeviceEcho`，所以直接构造三个设备的测试连 print 都不会发生。

关闭调试不等于"少看东西"：设备报的错仍然一个字不少地传上去，只是不再
回显正常回话。回显永远排在主控转述**之前**。
"""
from __future__ import annotations

import sys
import threading
import unicodedata

# 让 [GX28] / [CNC] / [伺服] 三个标签的右括号对齐（东亚全角算两列）。
LABEL_COLUMNS = 4


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(label: str) -> str:
    return label + " " * max(0, LABEL_COLUMNS - _display_width(label))


class DeviceEcho:
    """线程安全的逐行回显。`enabled=False` 时全是空操作。"""

    def __init__(self, *, stream=None, enabled: bool = True):
        self.stream = sys.stdout if stream is None else stream
        self.enabled = enabled
        self._lock = threading.Lock()
        self._prompt: str | None = None

    def tx(self, label: str, text: str) -> None:
        """主控发给设备的一行。"""
        self._line(f"[{_pad(label)}] > {text}")

    def rx(self, label: str, text: str) -> None:
        """设备回给主控的一行。"""
        self._line(f"[{_pad(label)}] < {text}")

    def hold_prompt(self, text: str) -> None:
        """记下 input() 正挂着的提示语，供回显后补画。"""
        self._prompt = text

    def release_prompt(self) -> None:
        self._prompt = None

    def _line(self, text: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            # input() 的提示语不带换行，回显行会直接粘在它后面；先 \r 擦掉整行。
            self.stream.write(f"\r{text}\n")
            if self._prompt:
                # 不补画的话，_read_stop_commands 阻塞在 input() 上时只要来一条
                # 回显，操作员就再也看不到"输入 ! 急停"——那是安全回归。
                self.stream.write(self._prompt)
            self.stream.flush()


class GrblTrace:
    """把 GrblController 的收发接到 DeviceEcho，并折叠重复的状态轮询。

    CNC 每次移动都会每 0.1 秒发一个 `?`，一秒 10 行一模一样的 `<Run|MPos:...>`。
    那些不是设备主动说的话，是心跳，所以只在**状态变化**时打一次；`ok`、
    `error:N`、`ALARM:N`、`[MSG:]` 一律原样打印。
    """

    def __init__(self, echo: DeviceEcho, label: str = "CNC"):
        self.echo = echo
        self.label = label
        self._last_state: str | None = None

    def tx(self, text: str) -> None:
        self.echo.tx(self.label, text)

    def rx(self, response) -> None:
        from cnc_grbl.protocol import ResponseKind

        if response.kind is ResponseKind.STATUS:
            state = (response.state or "").split(":", 1)[0]
            if state == self._last_state:
                return
            self._last_state = state
        self.echo.rx(self.label, response.raw)
