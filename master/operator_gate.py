"""Per-placement operator acknowledgement after wire cutting and reloading."""

from __future__ import annotations

import secrets
import sys
import threading
from typing import TextIO


class OperatorGate:
    def __init__(self, *, output: TextIO | None = None):
        self.output = output if output is not None else sys.stdout
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._token: str | None = None

    @property
    def waiting_token(self) -> str | None:
        with self._lock:
            return self._token

    def wait_for_ready(self, completed: int, total: int,
                       cancel: threading.Event) -> bool:
        token = secrets.token_hex(3).upper()
        with self._lock:
            self._ready.clear()
            self._token = token
        print(f"已完成 {completed}/{total} 次五针植入，GX28 已回待机点且上气阀 B 已排气。"
              f"请人工剪断五根丝并准备下一次植入；确认安全后输入 READY {token}。"
              "输入 ! 可停止。", file=self.output, flush=True)
        try:
            while not cancel.is_set():
                if self._ready.wait(0.05):
                    return not cancel.is_set()
            return False
        finally:
            with self._lock:
                self._token = None
                self._ready.clear()

    def submit(self, command: str) -> bool:
        with self._lock:
            if self._token is None or command.strip().upper() != f"READY {self._token}":
                return False
            self._ready.set()
            return True
