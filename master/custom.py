"""Flat custom controls, shared by the CLI and GUI; profiles never store state."""
from __future__ import annotations

import math
from copy import deepcopy
import os
import sys
import threading
from pathlib import Path

from master.coordinator import Coordinator, RunState
from master.local_config import load_merged
from master.model import MAX_DEPTH_MM, HeadMotion, MachineGeometry, validate_cut_clearance_mm
from master.planner import Placement, RowPlan
from master.wizard import WizardCancelled

_DEFAULTS = dict(needle_pitch_mm=10.0, travel_mm=2.0, x_mm=0.0, y_mm=0.0, z_mm=0.0)


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数")
    return float(value)


def servo_target_from_pitch(pitch, geometry=None):
    geometry = geometry or MachineGeometry(5, 10.0, 10.0, 13.3333333)
    pitch = _finite(pitch, "针距")
    if not geometry.station_width_mm <= pitch <= geometry.station_width_mm + geometry.max_expansion_mm:
        raise ValueError("针距超出机械标定范围")
    return -(pitch - geometry.station_width_mm)


def insertion_from_travel(travel, standby_mm=0.0, work_min_mm=-10.0):
    travel = _finite(travel, "下针行程")
    standby_mm = _finite(standby_mm, "待机位置")
    work_min_mm = _finite(work_min_mm, "工作下界")
    target = standby_mm - travel
    if travel <= 0 or not work_min_mm <= target < standby_mm:
        raise ValueError("下针行程必须为正，且目标须在已确认工作窗口内")
    return target


def custom_from_sections(sections):
    values = dict(_DEFAULTS)
    section = sections.get("custom", {})
    if not isinstance(section, dict):
        raise ValueError("[custom] 必须是参数表")
    for key in values:
        if key in section:
            values[key] = _finite(section[key], key)
    servo_target_from_pitch(values["needle_pitch_mm"])
    if values["travel_mm"] <= 0:
        raise ValueError("下针行程必须为正")
    return values


def custom_sections(values):
    return {"custom": custom_from_sections({"custom": values})}


def _load_config(store, selection, config_path):
    if selection.get("machine") or selection.get("custom"):
        return store.compose(Path(config_path), machine=selection.get("machine"),
                             custom=selection.get("custom"))
    return load_merged(Path(config_path))


class CustomController:
    def __init__(self, *, store, selection, config_path, debug=False,
                 device_factory=None, cancel_event=None, menu=None, prepared_config=None):
        self.store, self.selection = store, selection
        self.cfg = (deepcopy(prepared_config) if prepared_config is not None
                    else _load_config(store, selection, config_path))
        self.menu, self.debug = menu, debug
        self._cut_coordinator = None
        from master.master import LiveDevices, UnavailableDevice, _known_geometry
        self.geometry = _known_geometry()
        self.warnings = []
        self.values = self._initial_values(self.cfg)
        self.devices = LiveDevices(*(UnavailableDevice(name, "尚未连接")
                                     for name in ("伺服", "CNC", "滑台")))
        self._constructed = set()
        self._factory = device_factory
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._closed = False
        self._monitor_done = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_sent = False
        self._monitor = None
        if cancel_event is not None:
            self._monitor = threading.Thread(target=self._watch_stop, daemon=True)
            self._monitor.start()

    def _initial_values(self, cfg):
        """Recover each invalid UI field independently; movement validates its own limits."""
        section = cfg.get("custom", {})
        if not isinstance(section, dict):
            self._warn("[custom] 参数表无效；使用默认值，其他组件仍可操作")
            section = {}
        values = dict(_DEFAULTS)
        for key in values:
            if key not in section:
                continue
            try:
                parsed = custom_from_sections({"custom": {key: section[key]}})
                values[key] = parsed[key]
            except (ValueError, TypeError) as exc:
                self._warn(f"自定义参数 {key} 无效（{exc}）；已使用默认值 {values[key]:g}")
        return values

    def update(self, values):
        if not isinstance(values, dict) or set(values) - set(_DEFAULTS):
            raise ValueError("自定义参数仅允许针距、下针行程及 XYZ")
        candidate = dict(self.values)
        candidate.update(values)
        candidate = custom_from_sections({"custom": candidate})
        self.values = candidate
        return dict(candidate)

    def _motion(self, *, standby_only=False, for_injection=False):
        hd = self.cfg.get("head", {})
        if hd.get("datum_confirmed") is not True or not all(k in hd for k in ("standby_mm", "work_min_mm", "work_max_mm")):
            raise RuntimeError("请先在机器配置中确认滑台待机位与安全工作窗口")
        standby = _finite(hd["standby_mm"], "待机位置")
        work_min = _finite(hd["work_min_mm"], "工作下界")
        travel = standby - work_min if standby_only else self.values["travel_mm"]
        clearance = (validate_cut_clearance_mm(self.cfg.get("cycle", {}).get("cut_clearance_mm", 0.0))
                     if for_injection else 0.0)
        if clearance and standby+clearance > hd["work_max_mm"]:
            raise RuntimeError("剪丝上升空间尚未记录；请先在实机向导完成 GX28 -5 mm 准备、对刀及上升距离设置")
        if for_injection:
            safe = _finite(hd.get("cut_safe_mm", standby+clearance), "剪丝安全位置")
            if abs(safe-(standby+clearance)) > 0.05:
                raise RuntimeError("记录的剪丝安全位置与上升距离不一致；请重新对刀")
        return HeadMotion(standby, insertion_from_travel(travel,
                          standby, work_min), work_min,
                          hd["work_max_mm"], True, cut_clearance_mm=clearance)

    def _ensure_cut_finished(self):
        if self._cut_coordinator is not None:
            raise RuntimeError("正在等待剪丝；请先执行‘剪丝完成，恢复待机’，再移动或重新设零")

    def _check(self):
        if self._closed or self.cancel_event.is_set():
            raise RuntimeError("自定义控制已停止；请退出并重新确认机械零位")

    def _device(self, name):
        self._check()
        if name not in self._constructed:
            if self._factory is not None:
                self.devices = self._factory(cfg=self.cfg, geometry=self.geometry, motion=None, debug=self.debug)
                self._constructed.update(("servo", "cnc", "head"))
                for device in (self.devices.servo, self.devices.cnc, self.devices.head):
                    device.cancel_event = self.cancel_event
            else:
                from master.master import _open_live_component, _make_echo
                try:
                    device = _open_live_component(name, self.cfg, self.geometry, None,
                                                  echo=_make_echo(self.debug))
                except Exception as exc:
                    raise RuntimeError(f"{name} 配置或初始化失败：{exc}") from exc
                device.cancel_event = self.cancel_event
                setattr(self.devices, name, device)
                self._constructed.add(name)
        device = getattr(self.devices, name)
        self._check()
        if not device.connected:
            try:
                device.connect()
            except Exception as exc:
                from master.master import UnavailableDevice
                try:
                    device.close()
                except Exception:
                    pass
                unavailable = UnavailableDevice(name, str(exc))
                unavailable.cancel_event = self.cancel_event
                setattr(self.devices, name, unavailable)
                self._constructed.discard(name)
                raise RuntimeError(f"{name} 连接失败：{exc}") from exc
        if self.cancel_event.is_set():
            self._safe_stop(force=True)
        self._check()
        return device

    def _call(self, component, method, *args):
        if method in ("move_mm", "move_to", "jog_setup_mm", "zero", "set_zero", "home", "unlock") or (component == "head" and method == "disable"):
            self._ensure_cut_finished()
        device = self._device(component)
        self._check()
        return getattr(device, method)(*args)

    def servo_pitch(self, pitch=None):
        if pitch is not None:
            self.update({"needle_pitch_mm": pitch})
        return self._call("servo", "move_mm", servo_target_from_pitch(self.values["needle_pitch_mm"], self.geometry))

    def servo_enable(self): return self._call("servo", "enable")
    def servo_set_zero(self): return self._call("servo", "set_zero")
    def servo_release(self): return self._call("servo", "disable")
    def servo_reset_fault(self): return self._call("servo", "reset_fault")

    def servo_jog_setup(self, delta):
        delta = _finite(delta, "点动距离")
        if not 0 < abs(delta) <= 0.5:
            raise ValueError("设零前点动必须在 ±0.5 mm 内且不能为零")
        return self._call("servo", "jog_setup_mm", delta)

    def head_travel(self, travel=None):
        self._ensure_cut_finished()
        if travel is not None:
            self.update({"travel_mm": travel})
        motion = self._motion()
        head = self._device("head")
        head.motion = motion
        self._check()
        return head.move_mm(motion.insertion_mm)

    def head_enable(self): return self._call("head", "enable")
    def head_zero(self): return self._call("head", "zero")
    def head_release(self): return self._call("head", "disable")
    def head_stop(self): return self._call("head", "stop")

    def head_standby(self):
        self._ensure_cut_finished()
        motion = self._motion(standby_only=True)
        head = self._device("head")
        head.motion = motion
        self._check()
        return head.move_mm(motion.standby_mm)

    def valve(self, which, open_):
        if which not in ("a", "b") or not isinstance(open_, bool):
            raise ValueError("气阀必须为 a/b，状态必须为布尔值")
        return self._call("head", "valve", which, open_)

    def valves_off(self):
        self.valve("a", False)
        self.valve("b", False)

    def cnc_move_to(self, x_mm=None, y_mm=None, z_mm=None):
        self.update({f"{axis}_mm": value for axis, value in zip("xyz", (x_mm, y_mm, z_mm)) if value is not None})
        cn = self.cfg.get("cnc3d", {})
        for axis in "xyz":
            if not isinstance(cn, dict) or f"{axis}_max" not in cn:
                raise RuntimeError(f"请先设置 CNC {axis.upper()} 安全上限")
            limit = _finite(cn[f"{axis}_max"], f"CNC {axis.upper()} 上限")
            value = self.values[f"{axis}_mm"]
            if limit <= 0 or (axis in "xy" and value < 0) or value > limit:
                raise ValueError(f"CNC {axis.upper()} 目标超出工作范围")
        return self._call("cnc", "move_to", *(self.values[f"{axis}_mm"] for axis in "xyz"))

    def cnc_home(self): return self._call("cnc", "home")
    def cnc_unlock(self): return self._call("cnc", "unlock")
    def cnc_position(self): return self._call("cnc", "position")

    def inject_once(self):
        self._ensure_cut_finished()
        motion = self._motion(for_injection=True)  # New motion for every button press.
        if not motion.cut_clearance_mm and self.cfg.get("cycle", {}).get("upper_release_after_retract") is not True:
            raise RuntimeError("单次注入前必须确认回针后释放上夹持阀的时序")
        head = self._device("head")
        if not head.zero_confirmed:
            raise RuntimeError("单次注入前必须明确设置滑台机械零位")
        try:
            servo = self._device("servo")
        except Exception as exc:
            self._check()  # A real stop must never be mistaken for optional hardware.
            self._warn(f"伺服不可用：{exc}；本次保持当前针距，仅执行滑台与气阀动作")
            servo = self.devices.servo
        head.motion = motion
        pitch = self.values["needle_pitch_mm"]
        plan = RowPlan(pitch, servo_target_from_pitch(pitch, self.geometry), 0.0,
                       (Placement(0.0, 0.0, ()),))
        coordinator = Coordinator(servo, self.devices.cnc, head, motion, scan_z_mm=0.0,
                                  operator_confirmed=True,
                                  servo_required=False, warning=self._warn,
                                  valve_delay_s=self.cfg.get("cycle", {}).get("valve_delay_ms", 500) / 1000)
        coordinator.cancel_event = self.cancel_event
        for device in (servo, self.devices.cnc, head):
            device.cancel_event = self.cancel_event
        self._check()
        result = coordinator.run_single(plan)
        if result.state is RunState.WAITING_OPERATOR:
            self._cut_coordinator = coordinator
            message = (f"GX28 已额外上抬 {motion.cut_clearance_mm:g} mm，电机保持使能，B 阀保持夹紧。"
                       "剪丝后让双手离开，再执行‘剪丝完成，恢复待机’。")
            if self.menu is not None:
                self.menu.say(message)
            else:
                print(message)
            return result
        if result.state is not RunState.COMPLETE:
            self.stop()
            raise RuntimeError(result.error or "单次注入未完成")
        return result

    def finish_cut(self):
        self._check()
        coordinator = self._cut_coordinator
        if coordinator is None:
            raise RuntimeError("没有等待确认的剪丝动作")
        head = self.devices.head
        position = head.position_mm()
        if not head.zero_confirmed or not math.isfinite(position) or abs(position - coordinator.motion.cut_position_mm) > 0.05:
            raise RuntimeError("剪丝等待位置或滑台零位已改变；未自动下降，请重新核实机械位置")
        result = coordinator.finish_single_cut()
        if result.state is not RunState.COMPLETE:
            self.stop()
            raise RuntimeError(result.error or "剪丝后恢复待机失败")
        self._cut_coordinator = None
        return result

    def names(self): return self.store.list_names("custom")

    def save(self, name):
        path = self.store.save("custom", name, custom_sections(self.values))
        self.store.save_selection(machine=self.selection.get("machine"), custom=name)
        self.selection["custom"] = name
        return path

    def load(self, name):
        candidate = custom_from_sections(self.store.load("custom", name))
        self.store.save_selection(machine=self.selection.get("machine"), custom=name)
        self.values = candidate
        self.selection["custom"] = name
        return dict(candidate)

    def _warn(self, message):
        self.warnings.append(message)
        if self.menu is not None:
            self.menu.warn(message)
        else:
            print(f"警告：{message}")

    def _safe_stop(self, *, force=False):
        if self.devices is None:
            return
        with self._stop_lock:
            if self._stop_sent and not force:
                return
            for component, method, args in (("head", "stop", ()), ("cnc", "hold", ()),
                    ("servo", "disable", ()), ("head", "valve", ("a", False)), ("head", "valve", ("b", False))):
                device = getattr(self.devices, component)
                if device.connected:
                    try:
                        getattr(device, method)(*args)
                    except Exception as exc:
                        self._warn(f"停止 {component}/{method} 失败：{exc}")
            self._stop_sent = True

    def stop(self):
        self.cancel_event.set()
        self._safe_stop()

    def _watch_stop(self):
        while not self._monitor_done.is_set():
            if self.cancel_event.wait(0.05):
                self._safe_stop()
                return

    def close(self):
        if self._closed:
            return
        self._closed = True
        # The session can finish before the watcher gets its next timeslice.
        # Do not let teardown discard an already requested global stop.
        if self.cancel_event.is_set():
            self._safe_stop()
        self._monitor_done.set()
        if self._monitor is not None:
            self._monitor.join()
        if self.devices is not None:
            from master.master import _close_live_devices
            _close_live_devices(self.devices)


def _terminal_stop_reader():
    """Poll only during an action; never leave a reader competing with menu input."""
    if os.name != "nt" or sys.stdin is None or not sys.stdin.isatty():
        return None
    import msvcrt

    def read_stop():
        if not msvcrt.kbhit():
            return False
        key = msvcrt.getwch()
        return key in ("!", "\x03")
    return read_stop


def _run_hardware_action(controller, action, read_stop=None):
    """Keep the main thread responsive to stop keys while one adapter is waiting."""
    finished = threading.Event()
    result, errors = [], []
    def run():
        try:
            result.append(action())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=run, name="custom-action")
    worker.start()
    while not finished.is_set():
        try:
            if read_stop is not None and read_stop():
                controller.cancel_event.set()
            finished.wait(0.025)
        except KeyboardInterrupt:
            controller.cancel_event.set()
    worker.join()
    if errors:
        raise errors[0]
    return result[0]


def run_custom_menu(*, store, selection, config_path, debug=False, menu=None,
                    device_factory=None, command_reader=None, read_stop=None, prepared_config=None):
    if menu is None:
        from master.menu import make_menu
        menu = make_menu()
    controller = CustomController(store=store, selection=selection, config_path=config_path,
        debug=debug, menu=menu, device_factory=device_factory,
        prepared_config=prepared_config,
        cancel_event=command_reader.cancel_event if command_reader is not None else threading.Event())
    if read_stop is None and command_reader is None:
        read_stop = _terminal_stop_reader()
    def hardware(action):
        return _run_hardware_action(controller, action, read_stop)
    actions = (
        ("1", "修改针距与下针行程"), ("2", "伺服按针距移动"), ("3", "伺服使能"),
        ("4", "伺服当前位置设零"), ("5", "伺服释放"), ("6", "伺服故障复位"),
        ("7", "伺服设零前小步点动"), ("8", "滑台按行程下针"), ("9", "滑台回待机"),
        ("10", "滑台使能"), ("11", "滑台实际待机点设零"), ("12", "滑台释放"),
        ("13", "滑台急停"), ("14", "A 阀通气"), ("15", "A 阀排气"),
        ("16", "B 阀通气"), ("17", "B 阀排气"), ("18", "两阀排气"),
        ("19", "CNC 移动到 XYZ"), ("20", "CNC 回零"), ("21", "CNC 解锁"),
        ("22", "CNC 当前位置"), ("23", "单次注入（保持 CNC 当前坐标）"),
        ("24", "保存自定义配置"), ("25", "加载自定义配置"), ("26", "列出自定义配置"),
        ("27", "剪丝完成，松开上阀并恢复滑台待机"),
        ("!", "全机停止"), ("0", "退出（Q 也可退出）"))
    simple = dict(zip(("2", "3", "4", "5", "6", "8", "9", "10", "11", "12", "13",
                       "18", "20", "21", "23", "27", "!"),
                      (controller.servo_pitch, controller.servo_enable, controller.servo_set_zero,
                       controller.servo_release, controller.servo_reset_fault, controller.head_travel,
                       controller.head_standby, controller.head_enable, controller.head_zero,
                       controller.head_release, controller.head_stop, controller.valves_off,
                       controller.cnc_home, controller.cnc_unlock, controller.inject_once, controller.finish_cut, controller.stop)))
    try:
        menu.warn("每次上电必须重新核实实际机械零位；屏幕显示 0 不代表机械零位。")
        menu.warn(f"自定义下针行程不采用预设的 {MAX_DEPTH_MM:g} mm 深度上限，必须按实际工装核实行程。")
        while not controller.cancel_event.is_set():
            menu.say(f"自定义参数：{controller.values}")
            menu.say(f"共用剪丝额外抬升：{controller.cfg.get('cycle', {}).get('cut_clearance_mm', 0)} mm。"
                     "启用后单次植入会等待剪丝，选择27确认后松阀并下降；未确认不能开始下一次。")
            hd = controller.cfg.get("head", {})
            menu.say(f"滑台工作窗口：[{hd.get('work_min_mm', '未设置')}, "
                     f"{hd.get('work_max_mm', '未设置')}] mm；待机位：{hd.get('standby_mm', '未设置')} mm；"
                     f"已确认：{'是' if hd.get('datum_confirmed') is True else '否'}；动作中按 ! 或 Ctrl+C 全机停止。")
            try:
                choice = menu.choose("自定义控制（所有操作在本页）", actions)
                if choice in ("0", "q", "Q"):
                    return 0
                if choice in ("4", "11", "23"):
                    menu.ask("请确认实际机械位置、装丝和行程安全；回车执行，Q 退出")
                if choice in simple:
                    hardware(simple[choice])
                elif choice == "1":
                    controller.update({key: menu.ask_float(label, default=str(controller.values[key]))
                        for key, label in (("needle_pitch_mm", "针距 mm"), ("travel_mm", "下针行程 mm"))})
                elif choice == "7":
                    delta = menu.ask_float("小步点动 mm", minimum=-0.5, maximum=0.5)
                    hardware(lambda: controller.servo_jog_setup(delta))
                elif choice in ("14", "15", "16", "17"):
                    hardware(lambda: controller.valve("a" if choice in ("14", "15") else "b", choice in ("14", "16")))
                elif choice == "19":
                    xyz = [menu.ask_float(f"目标 {a.upper()} mm", default=str(controller.values[f"{a}_mm"])) for a in "xyz"]
                    hardware(lambda: controller.cnc_move_to(*xyz))
                elif choice == "22":
                    menu.say(str(hardware(controller.cnc_position)))
                elif choice == "24":
                    controller.save(menu.ask("保存名称"))
                elif choice == "25":
                    controller.load(menu.ask("加载名称"))
                elif choice == "26":
                    menu.say("、".join(controller.names()) or "尚无自定义配置")
            except WizardCancelled:
                return 0
            except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
                menu.warn(f"操作失败：{exc}")
        return 1
    finally:
        controller.close()
