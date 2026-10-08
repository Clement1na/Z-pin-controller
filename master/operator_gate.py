"""Per-placement operator acknowledgement after wire cutting and reloading."""

from __future__ import annotations

import sys
import threading
from typing import TextIO


class OperatorGate:
    def __init__(self, *, output: TextIO | None = None):
        self.output = output if output is not None else sys.stdout
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._waiting = False

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._waiting

    def wait_for_ready(self, completed: int, total: int,
                       cancel: threading.Event) -> bool:
        with self._lock:
            self._ready.clear()
            self._waiting = True
        print(f"已完成 {completed}/{total} 次植入动作，GX28 已回待机点且上气阀 B 已排气。"
              "请人工剪断已装金属丝并准备下一次植入；确认安全后按回车继续。"
              "输入 ! 可停止。", file=self.output, flush=True)
        try:
            while not cancel.is_set():
                if self._ready.wait(0.05):
                    return not cancel.is_set()
            return False
        finally:
            with self._lock:
                self._waiting = False
                self._ready.clear()

    def submit(self, command: str) -> bool:
        with self._lock:
            if not self._waiting or command.strip():
                return False
            self._ready.set()
            return True
