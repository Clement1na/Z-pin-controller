# -*- coding: utf-8 -*-
"""`TkMenu`：把 `Wizard` 的问句渲染成对话框。

向导、配置档案、组件手动控制三块**全都只通过一个 menu 对象说话**（`say` / `warn` /
`ask` / `ask_float` / `ask_yes_no` / `choose`），而这三个入口又都把 menu 留成了可注入
参数。所以图形界面不需要重写那三块里的任何一行流程，只要在这里实现同一个接口。

校验规则逐条照抄 `master/wizard.py`：**同一份输入在终端和窗口里必须给出同一个判断**，
包括报错文案。数值解析抽成了模块级纯函数，这样不用起窗口也能测。
"""
from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
from tkinter import ttk

from master.wizard import WizardCancelled

CANCEL_WORDS = ("Q", "QUIT", "取消")

_PAD = 10


def is_cancel(text: str, cancel_words=CANCEL_WORDS) -> bool:
    return text.strip().upper() in cancel_words


def parse_float(raw: str, *, minimum: float | None = None,
                maximum: float | None = None, unit: str = "mm",
                too_big: str | None = None) -> float:
    """与 `Wizard.ask_float` 里的 parse 逐字对应。"""
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"请输入数字，收到 {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError("请输入有限的数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"必须不小于 {minimum:g} {unit}")
    if maximum is not None and value > maximum:
        raise ValueError(too_big or f"必须不大于 {maximum:g} {unit}")
    return value


def parse_yes_no(raw: str) -> bool:
    """与 `Wizard.ask_yes_no` 里的 parse 逐字对应。"""
    folded = raw.strip().lower()
    if folded in ("y", "yes", "是", "1"):
        return True
    if folded in ("n", "no", "否", "0"):
        return False
    raise ValueError(f"请输入 y 或 n，收到 {raw!r}")


def check_cancel(answer: str, default: str | None,
                 cancel_words=CANCEL_WORDS) -> str:
    """取消词抛 `WizardCancelled`；空串取默认值——与 `Wizard.ask` 一致。"""
    if is_cancel(answer, cancel_words):
        raise WizardCancelled("操作员取消")
    if not answer and default is not None:
        return default
    return answer


class TkMenu:
    """与 `Wizard` 同接口的对话框引擎。

    `sink` 是日志队列（与 `QueueStream` 同一个），`say`/`warn` 直接投进去，于是它们
    和 `print()`、设备回显按发生顺序混在同一条流里——和终端里的观感一致。
    """

    def __init__(self, root: tk.Misc, sink, *, cancel_words=CANCEL_WORDS):
        self.root = root
        self.sink = sink
        self._cancel_words = cancel_words
        self._requests = queue.Queue()
        self.cancel_event = threading.Event()
        self._active_dialogs = []
        self.root.after(25, self._pump_requests)

    def _pump_requests(self):
        self.root.after(25, self._pump_requests)
        if self.cancel_event.is_set():
            for dialog in list(self._active_dialogs):
                dialog._cancel()
        try:
            fn, box, done = self._requests.get_nowait()
        except queue.Empty:
            return
        try:
            if self.cancel_event.is_set():
                raise WizardCancelled('操作员请求停止')
            box['value'] = fn()
        except BaseException as exc:
            box['error'] = exc
        finally:
            done.set()

    def _dialog(self, prompt):
        dialog = _Dialog(self.root, prompt)
        dialog._owner = self
        self._active_dialogs.append(dialog)
        return dialog

    # ---------------------------------------------------------------- 单向输出
    def say(self, message: str = "") -> None:
        self.sink.put(message)

    def warn(self, message: str) -> None:
        # 前缀与 Wizard.warn 一致，日志面板据此把这一行标成警告色。
        self.sink.put(f"  ! {message}")

    # ---------------------------------------------------------------- 编组
    def _on_main(self, fn):
        """把一次控件操作编组回 Tk 主线程，并等它做完再返回。

        Tk 不是线程安全的：工作线程里直接碰控件，轻则花屏重则直接崩。所有对话框都
        必须走这里。主线程自己调用时直接执行——否则会自己等自己，死锁。
        """
        if self.cancel_event.is_set():
            raise WizardCancelled('操作员请求停止')
        if threading.current_thread() is threading.main_thread():
            return fn()
        box: dict = {}
        done = threading.Event()

        self._requests.put((fn, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    # ---------------------------------------------------------------- 问句
    def ask(self, prompt: str, *, default: str | None = None) -> str:
        def dialog() -> str:
            box = self._dialog(prompt)
            with box:
                entry = box.add_entry(initial="" if default is None else str(default))
                box.add_buttons(ok=lambda: check_cancel(
                    entry.get().strip(), default, self._cancel_words))
            return box.result

        return self._on_main(dialog)

    def ask_float(self, prompt: str, *, minimum: float | None = None,
                  maximum: float | None = None, default: str | None = None,
                  unit: str = "mm", too_big: str | None = None) -> float:
        def dialog() -> float:
            box = self._dialog(prompt)
            with box:
                entry = box.add_entry(initial="" if default is None else str(default))
                # 校验不过就地报错、对话框不关：终端里是重问一遍，窗口里再弹一个
                # 对话框只会让人烦躁，判断规则本身没变。
                box.add_buttons(ok=lambda: parse_float(
                    entry.get(), minimum=minimum, maximum=maximum,
                    unit=unit, too_big=too_big))
            return box.result

        return self._on_main(dialog)

    def ask_yes_no(self, prompt: str, *, default: bool = True) -> bool:
        def dialog() -> bool:
            box = self._dialog(prompt)
            with box:
                box.add_answer_buttons(focus=default)
            return box.result

        return self._on_main(dialog)

    def choose(self, prompt: str, options, *, default: str | None = None) -> str:
        """把编号选项渲染成一列按钮，返回所选项的 key。

        编号仍由调用方显式给出（`[0]` 可以是"退出"），key 原样返回、按字符串比，
        与 `Wizard.choose` 一致。
        """
        def dialog() -> str:
            box = self._dialog(prompt)
            with box:
                for key, label in options:
                    box.add_choice(key, label, focus=key == default)
                box.add_buttons(show_ok=False)
            return box.result

        return self._on_main(dialog)


class _Dialog:
    """一个模态对话框：标题 + 若干控件 + 底部按钮。

    用法：

        box = _Dialog(root, title)
        with box:
            ... 加控件 ...
        return box.result

    `with` 的**块体只负责摆控件**，真正的等待发生在 `__exit__` 里——所以 `box.result`
    必须在 `with` 之外读，写在块内会在用户操作之前就被求值。
    """

    def __init__(self, root: tk.Misc, title: str):
        self.top = tk.Toplevel(root)
        self.top.title("Z-pin")
        self.top.resizable(False, False)
        self.top.transient(root)
        self.result = None
        self._closed = False
        self._cancelled = False
        self._default_action = None
        self._error = tk.StringVar(value="")

        frame = ttk.Frame(self.top, padding=_PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, wraplength=460,
                  justify="left").pack(anchor="w", pady=(0, _PAD))
        self._body = frame
        self._error_label = ttk.Label(frame, textvariable=self._error,
                                      foreground="#b00020", wraplength=460,
                                      justify="left")
        self._error_label.pack(anchor="w")
        self._buttons = ttk.Frame(frame)
        self._buttons.pack(fill="x", pady=(_PAD, 0))

    # --- 摆控件 -----------------------------------------------------------------
    def add_entry(self, *, initial: str = "") -> ttk.Entry:
        entry = ttk.Entry(self._body, width=32)
        entry.insert(0, initial)
        entry.pack(anchor="w")
        entry.focus_set()
        entry.select_range(0, "end")
        return entry

    def add_choice(self, key: str, label: str, *, focus: bool = False) -> None:
        button = ttk.Button(self._body, text=f"[{key}] {label}",
                            command=lambda: self._finish(key))
        button.pack(fill="x", pady=2)
        if focus:
            button.focus_set()
            self._default_action = lambda: self._finish(key)

    def add_buttons(self, *, ok=None, ok_label: str = "确定",
                    cancel_label: str = "取消", show_ok: bool = True,
                    show_cancel: bool = True) -> None:
        """确定 → `ok()` 的返回值（`None` 表示校验没过，不关窗口）；取消 → 中止。"""
        if show_ok:
            self._default_action = lambda: self._attempt(ok)
            ttk.Button(self._buttons, text=ok_label,
                       command=lambda: self._attempt(ok)).pack(side="right")
        if show_cancel:
            ttk.Button(self._buttons, text=cancel_label,
                       command=self._cancel).pack(side="right", padx=(0, 6))

    def add_answer_buttons(self, *, yes_label: str = "是",
                           no_label: str = "否", focus: bool = True) -> None:
        """是 → True，否 → False。对应 `ask_yes_no`，两者都不是"取消"。"""
        yes = ttk.Button(self._buttons, text=yes_label,
                         command=lambda: self._finish(True))
        no = ttk.Button(self._buttons, text=no_label,
                        command=lambda: self._finish(False))
        yes.pack(side="right")
        no.pack(side="right", padx=(0, 6))
        (yes if focus else no).focus_set()
        self._default_action = lambda: self._finish(focus)

    def show_error(self, message: str) -> None:
        self._error.set(message)
        self.top.bell()

    # --- 结果 -------------------------------------------------------------------
    def _attempt(self, ok) -> None:
        try:
            value = ok() if ok is not None else None
        except WizardCancelled:
            # 操作员在输入框里敲了取消词——和按取消键同义。
            self._cancel()
            return
        except ValueError as exc:
            self.show_error(str(exc))
            return
        if value is None:
            return
        self._finish(value)

    def _cancel(self) -> None:
        self._finish(None, cancelled=True)

    def _finish(self, value, *, cancelled: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = value
        self._cancelled = cancelled
        self.top.grab_release()
        self.top.destroy()

    # --- 生命周期 ---------------------------------------------------------------
    def __enter__(self) -> "_Dialog":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None and not self._closed:
            # 控件这时才摆完，居中与抓取都要等到这里。
            self.top.bind("<Escape>", lambda _event: self._cancel())
            self.top.bind("<Return>", lambda _event: self._press_default())
            self.top.protocol("WM_DELETE_WINDOW", self._cancel)
            self.top.update_idletasks()
            self._center()
            self.top.grab_set()
            self.top.wait_window(self.top)
        if not self._closed:
            self._closed = True
            self.top.grab_release()
            self.top.destroy()
        owner = getattr(self, '_owner', None)
        if owner is not None and self in owner._active_dialogs:
            owner._active_dialogs.remove(self)
        if exc_type is None and self._cancelled:
            raise WizardCancelled("操作员取消")
        return False

    def _press_default(self) -> None:
        """回车 = 点"确定"或"是"，对应终端里按回车接受默认值。"""
        if self._default_action is not None:
            self._default_action()

    def _center(self) -> None:
        parent = self.top.master
        if parent is None or not parent.winfo_exists():
            return
        x = parent.winfo_rootx() + (parent.winfo_width() - self.top.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
