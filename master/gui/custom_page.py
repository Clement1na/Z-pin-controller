# -*- coding: utf-8 -*-
"""Single-page custom controls; device calls belong to RunController's worker."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from master.gui.menu import parse_float

_ACTION_LABELS = {
    "servo_pitch": "针距移动", "servo_enable": "伺服使能", "servo_set_zero": "伺服设零",
    "servo_release": "伺服释放", "servo_reset_fault": "伺服故障复位", "servo_jog_setup": "伺服微动",
    "head_travel": "滑台下移", "head_standby": "滑台返回待机位", "head_enable": "滑台使能",
    "head_zero": "滑台设零", "head_release": "滑台释放", "head_stop": "滑台停止",
    "valve": "气阀切换", "valves_off": "全部气阀关闭", "cnc_move_to": "CNC 移动",
    "cnc_home": "CNC 回原点", "cnc_unlock": "CNC 解锁", "cnc_position": "CNC 位置查询",
    "inject_once": "单次植入", "save": "配置保存", "load": "配置加载", "names": "配置列表查询",
}


class _CustomSession:
    """Tk-free request/result bridge. No additional worker is created here."""

    def __init__(self, controller_factory, options):
        self.factory = controller_factory
        self.options = options
        self.requests = queue.Queue()
        self.results = queue.Queue()
        self.close_event = threading.Event()
        self.cancel_event = None

    def submit(self, action, args=(), *, values=None):
        if not self.close_event.is_set():
            self.requests.put((action, tuple(args), dict(values) if values is not None else None))

    def close(self):
        self.close_event.set()
        event = self.cancel_event
        if event is not None:
            event.set()

    def run(self, reader):
        controller = None
        self.cancel_event = reader.cancel_event
        if self.close_event.is_set():
            self.cancel_event.set()
        try:
            controller = self.factory(**self.options, cancel_event=self.cancel_event)
            head = controller.cfg.get("head", {})
            self.results.put(("ready", {"values": dict(controller.values), "window": {
                key: head.get(key) for key in ("work_min_mm", "work_max_mm", "standby_mm", "datum_confirmed")}}, None))
            while not (self.close_event.is_set() or self.cancel_event.is_set()):
                try:
                    action, args, values = self.requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self.close_event.is_set() or self.cancel_event.is_set():
                    break
                try:
                    if values is not None:
                        controller.update(values)
                    result = getattr(controller, action)(*args)
                    self.results.put((action, result, None))
                except Exception as exc:
                    self.results.put((action, None, exc))
            return 0
        finally:
            try:
                if controller is not None:
                    controller.close()
            finally:
                self.close_event.set()
                self.results.put(("closed", None, None))


def _controller_factory(**kwargs):
    from master.custom import CustomController
    return CustomController(**kwargs)


class CustomPage(tk.Toplevel):
    """Construct on Tk thread, then pass ``page.run`` to MainWindow._start."""

    def __init__(self, root, *, store, selection, config_path, debug=False,
                 menu=None, controller_factory=None):
        super().__init__(root)
        self.title("Z-pin 自定义控制（实机）")
        self.transient(root)
        self.menu = menu
        self._session = _CustomSession(controller_factory or _controller_factory, dict(
            store=store, selection=dict(selection), config_path=config_path,
            debug=debug, menu=menu))
        self._busy = True
        self._closing = False
        self._buttons = []
        self.values = {key: tk.StringVar(self, value=value) for key, value in (
            ("needle_pitch_mm", "10"), ("travel_mm", "2"),
            ("x_mm", "0"), ("y_mm", "0"), ("z_mm", "0"))}
        self.name = tk.StringVar(self)
        self.jog = tk.StringVar(self, value="0.1")
        self.status = tk.StringVar(self, value="正在加载参数与工作窗口…")
        self._build()
        self._sync_buttons()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, self._poll)

    def _group(self, parent, title, row, column):
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
        return frame

    def _entry(self, parent, label, variable):
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=2)
        ttk.Label(line, text=label).pack(side="left")
        ttk.Entry(line, width=12, textvariable=variable).pack(side="right")

    def _button(self, parent, label, action, capture=None):
        button = ttk.Button(parent, text=label, command=lambda: self._request(action, capture))
        button.pack(fill="x", pady=2)
        self._buttons.append(button)

    def _build(self):
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        for column in range(3):
            body.columnconfigure(column, weight=1)
        servo = self._group(body, "伺服 / 针距", 0, 0)
        self._entry(servo, "针距 mm", self.values["needle_pitch_mm"])
        self._button(servo, "移动到针距", "servo_pitch", lambda: (self._number("needle_pitch_mm", minimum=0),))
        for label, action in (("使能", "servo_enable"), ("当前位置设零", "servo_set_zero"),
                              ("释放", "servo_release"), ("复位故障", "servo_reset_fault")):
            self._button(servo, label, action)
        self._entry(servo, "设置微动 mm（±0.5 内）", self.jog)
        self._button(servo, "执行有符号微动", "servo_jog_setup", lambda: (
            parse_float(self.jog.get(), minimum=-0.5, maximum=0.5),))
        ttk.Label(servo, text="设零前确认机械位置；设零会改变参考基准。",
                  wraplength=260, foreground="#b00020").pack(anchor="w", pady=4)

        head = self._group(body, "GX28 滑台", 0, 1)
        self._entry(head, "滑台行程 mm", self.values["travel_mm"])
        self._button(head, "按行程下移", "head_travel", lambda: (self._number("travel_mm", minimum=0),))
        for label, action in (("返回待机位", "head_standby"), ("使能", "head_enable"),
                              ("当前位置设零", "head_zero"), ("释放", "head_release"), ("停止滑台", "head_stop")):
            self._button(head, label, action)
        ttk.Label(head, text="此处为滑台行程，受工作窗口约束；不是植入深度的 ≤3 mm 限制。\n设零前确认机械位置与工作窗口。",
                  wraplength=260, foreground="#b00020").pack(anchor="w", pady=4)
        self.window_label = ttk.Label(head, text="工作窗口：加载中…", wraplength=260)
        self.window_label.pack(anchor="w", pady=4)

        valves = self._group(body, "气阀", 0, 2)
        for which in ("A", "B"):
            for on, label in ((True, "开"), (False, "关")):
                self._button(valves, f"阀 {which} {label}", "valve", lambda w=which, v=on: (w.lower(), v))
        self._button(valves, "全部关闭", "valves_off")

        cnc = self._group(body, "CNC 绝对坐标", 1, 0)
        for key, label in (("x_mm", "X mm"), ("y_mm", "Y mm"), ("z_mm", "Z mm")):
            self._entry(cnc, label, self.values[key])
        self._button(cnc, "移动到 XYZ", "cnc_move_to", lambda: tuple(
            self._number(key) for key in ("x_mm", "y_mm", "z_mm")))
        for label, action in (("回原点", "cnc_home"), ("解锁", "cnc_unlock"), ("查询位置", "cnc_position")):
            self._button(cnc, label, action)

        config = self._group(body, "参数 / 配置", 1, 1)
        self._button(config, "按当前参数植入一次", "inject_once", self._all_values)
        self._entry(config, "配置名称", self.name)
        self._button(config, "保存当前参数", "save", lambda: (self._name(), self._all_values()))
        self._button(config, "加载配置", "load", lambda: (self._name(),))
        self._button(config, "列出配置", "names")
        self.names_label = ttk.Label(config, text="", wraplength=260)
        self.names_label.pack(anchor="w")

        controls = self._group(body, "会话", 1, 2)
        ttk.Button(controls, text="急停并结束会话", command=self.close).pack(fill="x", pady=2)
        ttk.Button(controls, text="关闭并清理设备", command=self.close).pack(fill="x", pady=2)
        ttk.Label(body, textvariable=self.status, wraplength=850).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=4, pady=8)

    def _number(self, key, **limits):
        return parse_float(self.values[key].get(), **limits)

    def _all_values(self):
        return {key: self._number(key, **({"minimum": 0} if key in (
            "needle_pitch_mm", "travel_mm") else {})) for key in self.values}

    def _name(self):
        name = self.name.get().strip()
        if not name:
            raise ValueError("请输入配置名称")
        return name

    def _request(self, action, capture=None):
        if self._busy or self._closing:
            return
        try:
            captured = capture() if capture is not None else ()
        except ValueError as exc:
            self._note(str(exc), error=True)
            return
        values = None
        if action == "inject_once":
            values, captured = captured, ()
        elif action == "save":
            captured, values = (captured[0],), captured[1]
        self._busy = True
        self._sync_buttons()
        self.status.set("执行中…")
        self._session.submit(action, captured, values=values)

    def _sync_buttons(self):
        for button in self._buttons:
            button.configure(state="disabled" if self._busy or self._closing else "normal")

    def _note(self, text, *, error=False):
        self.status.set(text)
        if self.menu is not None:
            (self.menu.warn if error else self.menu.say)(text)

    def _poll(self):
        while True:
            try:
                action, value, error = self._session.results.get_nowait()
            except queue.Empty:
                break
            if action == "closed":
                self.destroy()
                return
            self._busy = False
            label = _ACTION_LABELS.get(action, "操作")
            if error is not None:
                self._note(f"{label}失败：{error}", error=True)
            else:
                loaded = value.get("values", {}) if action == "ready" else value
                if action in ("ready", "load") and isinstance(loaded, dict):
                    for key, variable in self.values.items():
                        if key in loaded:
                            variable.set(str(loaded[key]))
                if action == "ready":
                    window = value["window"]
                    def mm(key):
                        number = window.get(key)
                        return "未设置" if number is None else f"{number:g} mm"
                    self.window_label.configure(text=(
                        f"工作窗口：{mm('work_min_mm')} 至 {mm('work_max_mm')}\n"
                        f"待机位：{mm('standby_mm')}；机械基准"
                        + ("已确认" if window.get("datum_confirmed") is True else "未确认")))
                if action == "names":
                    self.names_label.configure(text="、".join(map(str, value or [])) or "暂无配置")
                if action == "ready":
                    self._note("参数已加载；动作会连接设备并可能移动机构。")
                elif action == "inject_once":
                    self._note("单次植入完成。请剪断针丝后再点击下一次植入；不会自动继续。")
                else:
                    self._note(f"{label}完成" + (f"：{value}" if value is not None else ""))
            self._sync_buttons()
        self.after(50, self._poll)

    def close(self):
        self._closing = True
        self._session.close()
        self._sync_buttons()
        self.status.set("正在停止并清理设备…")

    def run(self, reader):
        """RunController entry point. Never invokes Tk from this worker."""
        return self._session.run(reader)
