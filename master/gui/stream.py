# -*- coding: utf-8 -*-
"""图形界面与终端之间的两个适配器：输出出去、指令进来。

这两块是**纯逻辑，不碰 Tk**，所以可以脱开窗口站测试。它们能成立，靠的是既有代码
把这两个方向都留成了接缝：

* 输出——`print()`、`DeviceEcho(stream=None)`、`OperatorGate(output=None)`、
  `RowTurnGate(output=None)` 全都在**运行期**才取 `sys.stdout`。界面只要在开跑之前
  把 `sys.stdout` 换掉，这些输出就自动流进日志面板，一行都不用改。
* 指令——`_read_stop_commands` 本身就是一个可替换的 callable，换成按钮投递的队列，
  下游那条分派循环（急停 `!` / 继续 空串 / 收工 `D`）原样复用。
"""
from __future__ import annotations

import queue
import threading

# 单行缓冲上限。正常输出都是按行来的，这个值只在设备吐出一长串没有换行的东西时兜底，
# 免得缓冲无限涨。
MAX_PENDING_CHARS = 8192


class QueueStream:
    """把写入的行投进队列，供 Tk 主线程轮询后贴进日志面板。

    行是**攒到换行才投递**的：`print("a")` 分两次 write（"a" 和 "\\n"），逐次投递会
    把一行拆成两条日志。所以这里先缓冲，见到换行才切出去。
    """

    def __init__(self, sink: queue.Queue, *, name: str = "stdout"):
        self.sink = sink
        self.name = name
        self.encoding = "utf-8"
        self.errors = "replace"
        self._lock = threading.Lock()
        self._pending = ""

    # --- 供 print()/DeviceEcho 使用 -------------------------------------------------
    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            # DeviceEcho._line 每行先写一个 \r 去擦终端提示行；日志面板不是终端，
            # 这个字符会原样显示成一个方块，所以在这里就丢掉。
            self._pending += text.replace("\r", "")
            self._emit_ready_lines()
        return len(text)

    def flush(self) -> None:
        """把没有换行结尾的残行也投出去，保证任何输出都不会卡在缓冲里。

        安全的理由：`print(..., flush=True)` 是先写内容、再写换行、最后才 flush，
        所以走到这里时缓冲里剩下的必然是"还没等到换行"的残行，投出去正是本意。
        """
        with self._lock:
            if self._pending:
                self.sink.put(self._pending)
                self._pending = ""

    def _emit_ready_lines(self) -> None:
        """调用方必须已经持锁。"""
        while True:
            index = self._pending.find("\n")
            if index < 0:
                break
            self.sink.put(self._pending[:index])
            self._pending = self._pending[index + 1:]
        if len(self._pending) > MAX_PENDING_CHARS:
            self.sink.put(self._pending)
            self._pending = ""

    # --- 供 sys.stdout 顶替时被问到的那些问题 ---------------------------------------
    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self):
        raise OSError(f"{self.name} 没有文件描述符")


class GuiCommandReader:
    """图形界面版的运行期指令来源，与 `_read_stop_commands` 同签名同语义。

    区别只有一处：那边阻塞在 `input()` 上，这边阻塞在按钮投递的队列上。投递的内容
    沿用终端里的那三个约定，所以 `_execute_live_job` 的分派循环一个字都不用动：

    * `"!"`  急停
    * `""`   确认继续（也可以用来应答排间确认）
    * `"D"`  排间正常收工

    另一个区别是**它会自己退出**。终端那个线程一旦阻塞在 `input()` 上就从外面叫不醒，
    只能当 daemon 放着；这里每 0.1 秒看一眼 `stopped`，运行结束就干净收摊。
    """

    def __init__(self) -> None:
        self._pending: queue.Queue[str] = queue.Queue()
        self.cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._cut_waiting = False

    @property
    def cut_waiting(self):
        with self._state_lock:
            return self._cut_waiting

    def set_cut_waiting(self, waiting):
        with self._state_lock:
            self._cut_waiting = bool(waiting)

    def post(self, command: str) -> bool:
        """界面线程调用：投一条指令给正在跑的那次运行。"""
        from master.operator_gate import REPEAT
        with self._state_lock:
            if command.strip().upper() == REPEAT and not self._cut_waiting:
                return False
            if command.strip().upper() == REPEAT or command.strip() == "":
                self._cut_waiting = False
        if command.strip() == '!':
            self.cancel_event.set()
        self._pending.put(command)
        return True

    def __call__(self, commands: queue.Queue, *, echo=None,
                 stopped: threading.Event | None = None) -> None:
        # `echo` 只用来兼容签名：终端里要靠它把被回显擦掉的提示行补画回来，图形界面
        # 没有提示行，所以这里不需要 hold_prompt/release_prompt。
        while True:
            try:
                line = self._pending.get(timeout=0.1)
            except queue.Empty:
                if stopped is not None and stopped.is_set():
                    return
                continue
            if stopped is not None and stopped.is_set():
                # 运行已经结束，这一行是敲给下一个界面的，不能当成指令吃掉。
                return
            commands.put(line.strip())
