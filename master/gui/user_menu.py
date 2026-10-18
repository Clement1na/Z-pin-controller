"""Compact forms for the user GUI; all device work stays on the worker thread."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading
import math
import tkinter as tk
from tkinter import ttk

from master.gui.menu import TkMenu
from master.local_config import load_merged
from master.model import PresetSpec, PRESET_ANGLES, PRESET_COUNTS
from master.user_interfaces import scan_interfaces, ranked_candidates, save_interface_selection


class InterfaceForm(ttk.Frame):
    def __init__(self, parent, *, config_path, store, selection, catalog, menu):
        super().__init__(parent)
        self.config_path, self.store, self.selection = config_path, store, selection
        self.catalog, self.menu = catalog, menu
        self.results = queue.Queue()
        self.closed = False
        self._poll_id = None
        self.bind('<Destroy>', self._on_destroy, add='+')
        self.names = store.list_names('machines') if store is not None else []
        self.machine = selection.get('machine') if selection.get('machine') in self.names else None
        self.profile = tk.StringVar(self, value=self.machine or '当前本机配置')
        self.save_as = tk.StringVar(self, value=self.machine or '')
        self.manual = tk.BooleanVar(self, value=False)
        self.info = tk.StringVar(self)
        self.variables = {key: tk.StringVar(self) for key in ('servo', 'cnc3d', 'head')}
        self.combos, self.maps = {}, {}
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text='机器配置').grid(row=0, column=0, sticky='w', pady=4)
        combo = ttk.Combobox(self, state='readonly', textvariable=self.profile,
                            values=('当前本机配置', *self.names), width=48)
        combo.grid(row=0, column=1, sticky='ew', pady=4)
        combo.bind('<<ComboboxSelected>>', self._load_profile)
        ttk.Label(self, text='配置名称（可选）').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Entry(self, textvariable=self.save_as).grid(row=1, column=1, sticky='ew', pady=4)
        for row, (key, label) in enumerate((('servo', '伺服有线网卡（可选）'),
                                           ('cnc3d', 'CNC串口'), ('head', 'GX28滑台串口')), 2):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
            field = ttk.Combobox(self, textvariable=self.variables[key], state='readonly', width=60)
            field.grid(row=row, column=1, sticky='ew', pady=5)
            self.combos[key] = field
        ttk.Checkbutton(self, text='手动填写未列出的接口', variable=self.manual,
                        command=self._toggle_manual).grid(row=5, column=0, columnspan=2, sticky='w', pady=4)
        self.refresh_button = ttk.Button(self, text='重新扫描接口', command=self.refresh)
        self.refresh_button.grid(row=6, column=0, sticky='w', pady=5)
        ttk.Label(self, textvariable=self.info, wraplength=540, foreground='#555555').grid(
            row=7, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.cfg = self._compose(self.machine)
        self._fill()

    def _compose(self, machine):
        return (self.store.compose(self.config_path, machine=machine, custom=self.selection.get('custom'))
                if self.store is not None else load_merged(self.config_path))

    def _load_profile(self, _event=None):
        machine = None if self.profile.get() == '当前本机配置' else self.profile.get()
        try:
            cfg = self._compose(machine)
        except Exception as exc:
            self.menu.warn(f'读取机器配置失败：{exc}')
            self.profile.set(self.machine or '当前本机配置')
            self.info.set(f'读取配置失败：{exc}')
            return
        self.machine, self.cfg = machine, cfg
        self.save_as.set(machine or '')
        self._fill()

    def _value(self, key):
        text = self.variables[key].get()
        return text if self.manual.get() else self.maps[key].get(text, '')

    def _fill(self, keep=None):
        keep = keep or {key: self.cfg.get(key, {}).get('adapter' if key == 'servo' else 'port', '')
                        for key in self.variables}
        for key in self.variables:
            current = keep.get(key, '')
            entries = {'未连接 / 跳过': ''}
            selected = '未连接 / 跳过'
            candidates = ranked_candidates(self.catalog, key, current)
            for device in candidates:
                if key == 'servo':
                    duplicate = sum(n.description == device.description for n in candidates) > 1
                    value = current if not duplicate and current in (device.name, device.description) else device.name
                    label = device.description
                    if duplicate:
                        label += f'（接口{device.index+1}）'
                else:
                    value = device.device
                    label = f'{device.device}  {device.description}'
                if current and (value.casefold() == current.casefold()):
                    label = '上次使用：' + label
                    selected = label
                entries[label] = value
            if key == 'servo' and not current and len(entries) == 2:
                selected = next(label for label in entries if entries[label])
            self.maps[key] = entries
            self.combos[key].configure(values=tuple(entries), state='normal' if self.manual.get() else 'readonly')
            self.variables[key].set(current if self.manual.get() else selected)
        text = (f'已隐藏{self.catalog.hidden_nics}个无线/虚拟网卡、'
                f'{self.catalog.hidden_ports}个蓝牙/非USB串口。上次仍可用的接口优先显示。')
        if self.catalog.errors:
            text += '\n' + '\n'.join(self.catalog.errors)
        self.info.set(text)

    def _toggle_manual(self):
        # Convert labels to raw values when entering manual mode.
        values = {key: self.maps[key].get(self.variables[key].get(), self.variables[key].get())
                  for key in self.variables}
        self._fill(values)

    def refresh(self):
        if self.closed:
            return
        self.refresh_button.configure(state='disabled')
        self.info.set('正在扫描接口…')
        def scan():
            self.results.put(scan_interfaces())
        threading.Thread(target=scan, daemon=True).start()
        self._poll_id = self.after(30, self._poll_scan)

    def _on_destroy(self, event):
        if event.widget is self:
            self.closed = True
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
                self._poll_id = None

    def _poll_scan(self):
        self._poll_id = None
        if self.closed or not self.winfo_exists():
            return
        try:
            catalog = self.results.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(30, self._poll_scan)
            return
        current = {key: self._value(key) for key in self.variables}
        self.catalog = catalog
        self._fill(current)
        self.refresh_button.configure(state='normal')
        for error in catalog.errors:
            self.menu.warn(error)

    def save(self):
        name = self.save_as.get().strip()
        if name and name != self.machine and name in self.names:
            raise ValueError(f'机器配置“{name}”已存在，请从上方列表选择后修改，或使用新名称')
        try:
            return save_interface_selection(self.config_path, self.store, self.selection,
                cfg=self.cfg, machine=self.machine, save_as=self.save_as.get(),
                adapter=self._value('servo'), cnc_port=self._value('cnc3d'), head_port=self._value('head'))
        except Exception as exc:
            raise ValueError(f'接口配置未保存：{exc}') from exc


class UserMenu(TkMenu):
    def configure_interfaces(self, config_path, store, selection, catalog):
        def dialog():
            box = self._dialog('选择当前机器和实际接口。保存后，实机运行和完全自定义共用这些设置。')
            form = InterfaceForm(box._body, config_path=config_path, store=store,
                                 selection=selection, catalog=catalog, menu=self)
            form.pack(fill='x', before=box._error_label)
            try:
                with box:
                    box.add_buttons(ok=form.save, ok_label='保存接口配置')
                self.say('接口与机器配置已保存；尚未连接或移动设备。')
                return box.result
            finally:
                form.closed = True
        return self._on_main(dialog)

    def job_parameters(self, defaults):
        def dialog():
            box = self._dialog('设置本次植入任务：20 mm强化区，仅安装最右两针，机械针距10 mm。')
            body = ttk.Frame(box._body)
            body.pack(fill='x', before=box._error_label)
            count = tk.StringVar(body, value=str(defaults.get('count', 7) if defaults.get('count', 7) in PRESET_COUNTS else 7))
            angle = tk.StringVar(body, value=f"{defaults.get('angle_deg', 0) if defaults.get('angle_deg', 0) in PRESET_ANGLES else 0:g}")
            try:
                depth_value = float(defaults.get('depth_mm', 3))
                if not math.isfinite(depth_value) or depth_value <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                self.warn('已保存深度无效，本次先显示默认3 mm，请重新核对')
                depth_value = 3
            depth = tk.StringVar(body, value=f"{depth_value:g}")
            for row, (label, var, values) in enumerate((('每排根数', count, PRESET_COUNTS),
                                                       ('实际架角 °', angle, PRESET_ANGLES),
                                                       ('植入深度 mm（垂直管面）', depth, None))):
                ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=5)
                field = (ttk.Entry(body, textvariable=var) if values is None else
                         ttk.Combobox(body, state='readonly', textvariable=var, values=tuple(str(v) for v in values)))
                field.grid(row=row, column=1, sticky='ew', pady=5)
            with box:
                box.add_buttons(ok=lambda: PresetSpec(float(angle.get()), int(count.get()), float(depth.get())),
                                ok_label='下一步：连接设备')
            return box.result
        return self._on_main(dialog)

    def confirm_run(self, summary, *, can_save):
        def dialog():
            box = self._dialog('准备就绪，请核对本次参数：\n' + summary)
            save = tk.BooleanVar(box.top, value=can_save)
            if can_save:
                ttk.Checkbutton(box._body, text='保存本次对刀点，下次可选择复用', variable=save).pack(
                    anchor='w', before=box._error_label)
            with box:
                box.add_buttons(ok=lambda: {'start': True, 'save_touch_off': save.get()}, ok_label='开始植入')
                only_save = ttk.Button(box._buttons, text='仅保存，暂不运行',
                    command=lambda: box._finish({'start': False, 'save_touch_off': save.get()}))
                only_save.pack(side='left')
            return box.result
        return self._on_main(dialog)
