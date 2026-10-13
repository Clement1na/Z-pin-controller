# -*- coding: utf-8 -*-
"""主窗口：顶层菜单的图形版。

这个窗口就是 `master/menu.py` 那个编号菜单的等价物，按钮与它一一对应（实机运行 /
仿真 / 自检 / 调试输出 / 配置档案 / 完全自定义）。**它不重写任何业务逻辑**——五个
入口全部走既有函数，只是把"说话的对象"换成了 `TkMenu`：

    实机运行 → run_setup_wizard(..., wizard=menu, menu=menu, command_reader=reader)
    仿真     → collect_preset(menu, ...) → run_simulation(row)
    自检     → run_simulation(PresetSpec(**SMOKE_JOB), rows=SMOKE_ROWS)
    配置档案 → run_profile_manager(store, menu, ...)
    完全自定义 → CustomPage → CustomController

急停/继续/收工三条指令投给 `RunController`，由它转进既有那条分派循环，安全路径
（`head.stop()` + `cnc.hold()` + `servo.disable()`）一行没动。
"""
from __future__ import annotations

import queue
from copy import deepcopy
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk

from master.gui.menu import TkMenu
from master.gui.runner import CONTINUE, FINISH_ROW, REPEAT, STOP, RunController
from master.profiles import ProfileStore
from master.wizard import WizardCancelled

# 日志轮询间隔。50 毫秒对眼睛就是"实时"，又不至于把主线程烧掉。
POLL_MS = 50
# 单次最多贴多少行，防止设备一口气吐几千行时界面卡住。
MAX_LINES_PER_TICK = 400


def prefer_chinese_font(root: tk.Misc) -> None:
    """Tk 的默认字体在 Windows 上是 Segoe UI，中文靠回退字体凑合。

    换成雅黑不是审美问题：设备回显里 `[GX28]` 这类方括号和中文混排时，回退字体的
    字宽不一致会让整列对不齐，而"对齐"正是回显可读性的全部。
    """
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
        if name in families:
            for key in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                        "TkHeadingFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(key).configure(family=name, size=10)
                except tk.TclError:
                    pass
            return


class MainWindow:
    def __init__(self, root: tk.Tk, sink: queue.Queue, *,
                 config_path: Path, results_dir: Path | None = None):
        self.root = root
        self.sink = sink
        self.config_path = Path(config_path)
        self.results_dir = results_dir
        self._closing = False
        self._prepared_config = None
        self._configuration_results = queue.Queue()
        self.menu = TkMenu(root, sink)
        self.store = ProfileStore(self.config_path.parent / "profiles")
        self.selection = self.store.load_selection()
        self.debug_var = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="空闲")
        # 工作线程只能往队列里放东西；碰控件一律由下面的轮询做。
        self.finished: queue.Queue = queue.Queue()
        # 不能直接写 `on_finished=self.finished.put`：`put(item, block=True)` 收两个
        # 位置参数，于是 `put(code, error)` 会静默地把 `error` 当成 block 标志丢掉，
        # 队列里只剩一个 int——`_drain_finished` 解包时抛 TypeError，轮询链当场断掉。
        self.runner = RunController(on_finished=self._finish_run)

        prefer_chinese_font(root)
        self._build()
        self._refresh_heading()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump()

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        self.root.title("Z-pin 五针植入控制")
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self.heading = ttk.Label(outer, text="")
        self.heading.pack(anchor="w")

        modes = ttk.Frame(outer)
        modes.pack(fill="x", pady=(8, 0))
        for label, command in (
            ("接口与机器配置", self._configure_interfaces),
            ("实机运行（会连接并可能移动机构）", self._live),
            ("仿真（不接硬件）", self._simulate),
            ("自检", self._smoke),
            ("配置档案", self._profiles),
            ("预设任务", self._preset),
            ("完全自定义", self._custom),
        ):
            ttk.Button(modes, text=label, command=command).pack(side="left", padx=(0, 6))

        ttk.Checkbutton(outer, text="调试输出（回显设备每一行）",
                        variable=self.debug_var).pack(anchor="w", pady=(8, 0))

        self.log = scrolledtext.ScrolledText(outer, height=22, width=96,
                                             wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.tag_config("warn", foreground="#b00020")
        self.log.tag_config("echo", foreground="#6a6a6a")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.continue_button = ttk.Button(
            bottom, text="继续", state="disabled",
            command=lambda: self._post(CONTINUE, "已发送确认继续。"))
        self.continue_button.pack(side="left")
        self.repeat_button = ttk.Button(bottom, text="原位重做本次植入", state="disabled",
                                        command=self._repeat_current)
        self.repeat_button.pack(side="left", padx=(6, 0))
        self.finish_button = ttk.Button(
            bottom, text="排间收工", state="disabled",
            command=lambda: self._post(FINISH_ROW, "已请求排间正常收工。"))
        self.finish_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(bottom, text="急停", state="disabled",
                                      command=self._stop)
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(bottom, textvariable=self.status).pack(side="right")
        ttk.Button(bottom, text="清空日志",
                   command=self._clear_log).pack(side="right", padx=(0, 12))

    def _refresh_heading(self) -> None:
        machine = self.selection.get("machine") or "未选择"
        custom = self.selection.get("custom") or "未选择"
        self.heading.configure(text=f"机器配置：{machine}    自定义配置：{custom}")

    # ------------------------------------------------------------------ 轮询
    def _pump(self) -> None:
        try:
            self._drain_log()
            self._drain_finished()
            self._drain_configuration()
            self._sync_buttons()
            if self._closing and not self.runner.running:
                self._destroyed = True
                self.root.destroy()
                return
        finally:
            # 续下一跳放在 finally 里：这一跳要是抛了异常还不再排，界面就永久停在
            # 最后一帧——按钮看着可用、点下去没反应，而设备可能还在动。
            if not getattr(self, '_destroyed', False):
                self.root.after(POLL_MS, self._pump)

    def _finish_run(self, code: int, error: BaseException | None) -> None:
        """`RunController` 的结束回调。在工作线程上被调用，所以只许碰队列。"""
        self.finished.put((code, error))

    def _drain_log(self) -> None:
        lines = []
        for _ in range(MAX_LINES_PER_TICK):
            try:
                lines.append(self.sink.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log.configure(state="normal")
        for line in lines:
            self.log.insert("end", f"{line}\n", _tag_for(line))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_finished(self) -> None:
        while True:
            try:
                code, error = self.finished.get_nowait()
            except queue.Empty:
                return
            if error is not None:
                self._note(f"运行失败：{error}")
            else:
                self._note(f"运行结束，退出码 {code}。")
            selection = self.store.load_selection()
            if selection != self.selection:
                self._prepared_config = None
            self.selection = selection
            self._refresh_heading()

    def _sync_buttons(self) -> None:
        state = "normal" if self.runner.running else "disabled"
        for button in (self.stop_button, self.continue_button, self.finish_button):
            if str(button.cget("state")) != state:
                button.configure(state=state)
        if hasattr(self, "repeat_button"):
            self.repeat_button.configure(state="normal" if self.runner.cut_waiting else "disabled")
        wanted = "运行中" if self.runner.running else "空闲"
        if self.status.get() != wanted:
            self.status.set(wanted)

    # ------------------------------------------------------------------ 动作
    def _note(self, text: str) -> None:
        self.sink.put(text)

    def _start(self, flow, label: str) -> None:
        if self.runner.running:
            self._note("已经有一次运行在进行中，等它结束。")
            return
        self._note(f"—— {label} ——")
        def wrapped(reader):
            self.menu.cancel_event = reader.cancel_event
            return flow(reader)
        self.runner.start(wrapped)

    def _post(self, command: str, message: str) -> None:
        if self.runner.post(command):
            self._note(message)

    def _repeat_current(self):
        if not self.runner.cut_waiting:
            self._note("仅在等待剪丝时可原位重做。")
            return
        try:
            confirmed = self.menu.ask_yes_no(
                "原位重做本次植入？请先处理好金属丝，确认当前点可重做并让双手离开。"
                "CNC和伺服不重新定位，沿用本次下针参数，完成后再次等待剪丝。", default=False)
        except WizardCancelled:
            return
        if confirmed:
            self._post(REPEAT, "已请求原位重做本次植入；完成后仍需剪丝确认。")

    def _stop(self) -> None:
        if not self.runner.post(STOP):
            # Settings may have returned just before this click, while their
            # requested branch is still waiting for the next Tk poll.
            while True:
                try:
                    _branch, _cfg, _selection, cancel_event = self._configuration_results.get_nowait()
                except queue.Empty:
                    break
                cancel_event.set()
                self._prepared_config = None
            self._note("当前没有在运行。")
            return
        self._note("已请求急停：滑台急停、CNC 保持、伺服卸使能由总控按既有安全路径执行。")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ 五个入口
    def _live(self) -> None:
        self._configured_branch("live")

    def _configured_branch(self, branch):
        self._drain_configuration()
        if self.runner.running:
            self._note("已有流程正在运行。")
            return
        if self._prepared_config is None:
            self._configure_interfaces(branch)
        else:
            self._launch_configured(branch, deepcopy(self._prepared_config))

    def _configure_interfaces(self, branch=None):
        if self.runner.running:
            self._note("已有流程正在运行。")
            return
        self._prepared_config = None
        selection = dict(self.selection)
        from master.interfaces import prepare_interfaces

        def flow(reader):
            try:
                cfg = prepare_interfaces(self.config_path, self.menu,
                                         store=self.store, selection=selection)
            except WizardCancelled:
                self._note("接口与机器配置已取消。")
                return 0
            self._configuration_results.put((branch, deepcopy(cfg), dict(selection), reader.cancel_event))
            return 0

        self._start(flow, "接口与机器配置（不连接设备）")

    def _drain_configuration(self):
        if self.runner.running:
            return
        try:
            branch, cfg, selection, cancel_event = self._configuration_results.get_nowait()
        except queue.Empty:
            return
        if self._closing or cancel_event.is_set() or cfg is None:
            self._prepared_config = None
            return
        self.selection = selection
        self._prepared_config = deepcopy(cfg)
        self._refresh_heading()
        self._note("接口与机器配置已就绪；实机和自定义共用本次选择。")
        if branch is not None:
            self._launch_configured(branch, deepcopy(cfg))

    def _launch_configured(self, branch, prepared_config):
        if branch == "custom":
            from master.gui.custom_page import CustomPage
            page = CustomPage(self.root, store=self.store, selection=dict(self.selection),
                              config_path=self.config_path, debug=self.debug_var.get(), menu=self.menu,
                              prepared_config=prepared_config,
                              on_configuration_changed=self._invalidate_configuration)
            self._start(page.run, "完全自定义")
            return
        from master.wizard import run_setup_wizard

        debug = self.debug_var.get()
        selection = dict(self.selection)
        config_path = self.config_path
        results_dir = self.results_dir

        def flow(reader):
            try:
                return run_setup_wizard(
                    config_path, results_dir=results_dir, debug=debug,
                    wizard=self.menu, menu=self.menu, command_reader=reader,
                    profile_store=self.store, profile_selection=selection,
                    prepared_config=prepared_config)
            finally:
                self._cache_persisted_configuration(reader)

        self._start(flow, "实机运行（会连接并可能移动机构）")

    def _invalidate_configuration(self):
        self._prepared_config = None

    def _cache_persisted_configuration(self, reader):
        """Worker-side refresh after the live wizard persists calibration changes."""
        from master.local_config import load_merged
        try:
            cfg = load_merged(self.config_path)
            selection = self.store.load_selection()
        except Exception as exc:
            self._note(f"无法刷新共享配置：{exc}；下次运行将重新选择。")
            cfg, selection = None, {}
        self._configuration_results.put((None, deepcopy(cfg), dict(selection), reader.cancel_event))

    def _simulate(self) -> None:
        if self.runner.running:
            self._note('已有流程正在运行。')
            return
        self.menu.cancel_event.clear()
        from master.master import _known_geometry, run_simulation
        from master.wizard import collect_preset

        # 参数问答放在主线程做：它全是模态对话框，本来就要阻塞，而且这样
        # `WizardCancelled` 不会跑进工作线程。
        try:
            row, _plan = collect_preset(self.menu, _known_geometry())
        except WizardCancelled as exc:
            self._note(f"已取消（{exc}），未开始仿真。")
            return
        self._start(lambda _reader: run_simulation(row), "仿真（不接硬件）")

    def _smoke(self) -> None:
        from master.master import SMOKE_JOB, SMOKE_ROWS, run_simulation
        from master.model import PresetSpec

        row = PresetSpec(**SMOKE_JOB)
        self._start(lambda _reader: run_simulation(row, rows=SMOKE_ROWS), "自检")

    def _profiles(self) -> None:
        if self.runner.running:
            self._note('已有流程正在运行。')
            return
        self.menu.cancel_event.clear()
        self._prepared_config = None
        from master.master import _known_geometry
        from master.profile_ui import run_profile_manager

        # 纯交互，没有设备动作，直接在主线程走完即可。
        try:
            self.selection = run_profile_manager(
                self.store, self.menu, self.config_path)
        except WizardCancelled:
            pass
        self._refresh_heading()

    def _preset(self) -> None:
        from master.menu import run_preset_task
        debug = self.debug_var.get()
        selection = dict(self.selection)
        prepared_config = deepcopy(self._prepared_config)
        def flow(reader):
            try:
                return run_preset_task(config_path=self.config_path, menu=self.menu,
                                       results_dir=self.results_dir, debug=debug,
                                       profile_store=self.store, profile_selection=selection,
                                       command_reader=reader, prepared_config=prepared_config)
            finally:
                if prepared_config is not None:
                    self._cache_persisted_configuration(reader)
        self._start(flow, "预设任务")

    def _custom(self) -> None:
        self._configured_branch("custom")

    # ------------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        if self.runner.running:
            if not messagebox.askyesno(
                    "Z-pin", "正在运行。退出会先请求急停，确定退出吗？"):
                return
            self.runner.post(STOP)
            self._closing = True
            self._note('正在停止并等待设备收尾；完成后关闭窗口。')
            return
        self._destroyed = True
        self.root.destroy()


def _tag_for(line: str) -> tuple:
    if line.startswith("  ! "):
        return ("warn",)
    if line.startswith("[") and ("] > " in line or "] < " in line):
        return ("echo",)
    return ()
