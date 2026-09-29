"""Command-line component control for commissioning and fault finding.

The manual pages intentionally sit above the device adapters: they reuse the
same safety windows, zeroing commands, valve protocol, and one-shot cycle as
automatic mode.  No page issues a CNC move unless the operator explicitly asks
for one.
"""

from __future__ import annotations

import math
from pathlib import Path

from master.coordinator import Coordinator, RunState
from master.local_config import load_merged
from master.model import HeadMotion, MachineGeometry, RowSpec
from master.planner import plan_row
from master.profiles import ProfileStore
from master.wizard import WizardCancelled


def _load_config(store: ProfileStore, selection: dict, config_path: Path) -> dict:
    machine = selection.get("machine")
    job = selection.get("job")
    if machine or job:
        return store.compose(config_path, machine=machine, job=job)
    return load_merged(config_path)


def _row_from_config(cfg: dict) -> RowSpec | None:
    values = cfg.get("job", {})
    if not all(key in values for key in ("angle_deg", "length_mm", "spacing_mm", "depth_mm")):
        return None
    return RowSpec(**{key: values[key]
                      for key in ("angle_deg", "length_mm", "spacing_mm", "depth_mm")})


def _manual_motion(cfg: dict, row: RowSpec | None) -> HeadMotion:
    head = cfg.get("head", {})
    standby = float(head.get("standby_mm", 0.0))
    work_min = float(head.get("work_min_mm", -10.0))
    work_max = float(head.get("work_max_mm", standby))
    if row is None:
        insertion = max(work_min, min(standby - 1.0, -1.0))
    else:
        insertion = -row.slide_travel_mm
    if not work_min <= insertion < standby:
        insertion = work_min
    return HeadMotion(standby_mm=standby, insertion_mm=insertion,
                      work_min_mm=work_min, work_max_mm=work_max,
                      datum_confirmed=True)


def _default_factory(*, cfg, geometry, motion, debug=False):
    from master.master import _make_echo, _open_live_devices
    return _open_live_devices(cfg, geometry, motion, echo=_make_echo(debug))


def _connect(device, label: str) -> None:
    if getattr(device, "connected", False):
        return
    connect = getattr(device, "connect", None)
    if connect is None:
        raise RuntimeError(f"{label} 没有连接接口")
    connect()


def _close(device) -> None:
    close = getattr(device, "close", None)
    if close is not None:
        close()


def _ask_signed(menu, prompt: str, *, minimum=None, maximum=None, default=None) -> float:
    return menu.ask_float(prompt, minimum=minimum, maximum=maximum, default=default)


def _servo_status(menu, servo) -> None:
    def attr(name):
        try:
            return getattr(servo, name)
        except Exception as exc:
            return f"不可读（{exc}）"

    raw = attr("raw_position_mm")
    setup = attr("setup_offset_mm")
    logical = attr("position_mm")
    def read(value):
        try:
            return value() if callable(value) else value
        except Exception as exc:
            return f"不可读（{exc}）"
    menu.say(f"伺服：连接={'是' if servo.connected else '否'}，"
             f"使能={'是' if getattr(getattr(servo, 'dev', None), 'enable', False) else '未知'}，"
             f"上电相对={read(setup)}，逻辑位置={read(logical) if logical is not None else '未设零'}，"
             f"原始位置={read(raw) if raw is not None else '不可读'}")


def _servo_page(menu, servo, geometry) -> None:
    _connect(servo, "SV630N 伺服")
    while True:
        choice = menu.choose("伺服操作", (
            ("1", "查看状态"),
            ("2", "使能"),
            ("3", "小步点动（设零前）"),
            ("4", "把当前位置设为零位"),
            ("5", "移动到绝对间距标定值"),
            ("6", "释放使能"),
            ("7", "复位故障"),
            ("0", "返回"),
        ))
        if choice == "0":
            return
        if choice == "1":
            _servo_status(menu, servo)
        elif choice == "2":
            servo.enable(); menu.say("伺服已使能。")
        elif choice == "3":
            delta = _ask_signed(menu, "点动距离（正=靠紧，负=张开，-0.5..0.5 mm）",
                                minimum=-0.5, maximum=0.5)
            servo.jog_setup_mm(delta); menu.say(f"已点动 {delta:+g} mm。")
        elif choice == "4":
            servo.set_zero(); menu.say("当前点已记录为伺服逻辑零位。")
        elif choice == "5":
            target = _ask_signed(menu, "目标标定值（0..-10 mm）",
                                 minimum=-geometry.max_expansion_mm, maximum=0.0)
            servo.move_mm(target); menu.say(f"已移动到 {target:+g} mm。")
        elif choice == "6":
            servo.disable(); menu.say("伺服已释放。")
        elif choice == "7":
            servo.reset_fault(); menu.say("已发送伺服故障复位。")


def _cnc_page(menu, cnc) -> None:
    _connect(cnc, "CNC 三维模组")
    while True:
        choice = menu.choose("CNC 操作", (
            ("1", "查看当前位置"),
            ("2", "移动到 XYZ"),
            ("3", "执行回零"),
            ("4", "解除报警"),
            ("0", "返回"),
        ))
        if choice == "0":
            return
        if choice == "1":
            menu.say(f"CNC 当前位置：{cnc.position()}")
        elif choice == "2":
            current = cnc.position() or (0.0, 0.0, 0.0)
            x = _ask_signed(menu, "目标 X mm", minimum=0.0, maximum=cnc.limits[0], default=str(current[0]))
            y = _ask_signed(menu, "目标 Y mm", minimum=0.0, maximum=cnc.limits[1], default=str(current[1]))
            z = _ask_signed(menu, "目标 Z mm（只限制上界）", maximum=cnc.limits[2], default=str(current[2]))
            cnc.move_to(x, y, z); menu.say("CNC 已到位。")
        elif choice == "3":
            cnc.home(); menu.say("已发送 CNC 回零。")
        elif choice == "4":
            cnc.unlock(); menu.say("已发送 CNC 解锁。")


def _head_page(menu, head, motion) -> None:
    _connect(head, "GX28 滑台")
    while True:
        choice = menu.choose("GX28 滑台操作", (
            ("1", "查看位置"),
            ("2", "使能电机"),
            ("3", "当前点设为零位"),
            ("4", "移动到目标位置"),
            ("5", "释放电机"),
            ("6", "急停"),
            ("0", "返回"),
        ))
        if choice == "0":
            return
        if choice == "1":
            menu.say(f"GX28 位置：{head.position_mm():+.3f} mm；零位已确认：{head.zero_confirmed}")
        elif choice == "2":
            head.enable(); menu.say("GX28 电机已使能。")
        elif choice == "3":
            menu.say("请确认滑台位于实际待机点；按回车发送零位命令。")
            menu.ask("确认")
            head.zero(); menu.say("当前点已设为 GX28 软件零位。")
        elif choice == "4":
            target = _ask_signed(menu, "目标位置 mm",
                                 minimum=motion.work_min_mm, maximum=motion.work_max_mm)
            head.move_mm(target); menu.say(f"GX28 已移动到 {target:+g} mm。")
        elif choice == "5":
            head.disable(); menu.say("GX28 电机已释放。")
        elif choice == "6":
            head.stop(); menu.warn("已发送急停；位置已不可信，请重新设零。")


def _valve_page(menu, head) -> None:
    _connect(head, "GX28 滑台（气阀）")
    while True:
        choice = menu.choose("气阀操作", (
            ("1", "A 阀通气"), ("2", "A 阀排气"),
            ("3", "B 阀通气"), ("4", "B 阀排气"),
            ("5", "两阀全部排气"), ("0", "返回"),
        ))
        if choice == "0":
            return
        if choice == "1":
            head.valve("a", True)
        elif choice == "2":
            head.valve("a", False)
        elif choice == "3":
            head.valve("b", True)
        elif choice == "4":
            head.valve("b", False)
        else:
            head.valve("a", False); head.valve("b", False)
        menu.say("气阀命令已发送。")


def _single_injection(menu, devices, geometry, motion, row, cfg) -> None:
    if row is None:
        raise RuntimeError("尚未选择作业方案；请先在配置档案中创建并选择作业方案")
    _connect(devices.servo, "SV630N 伺服")
    _connect(devices.head, "GX28 滑台")
    if getattr(devices.servo, "origin_count", None) is None or not devices.head.zero_confirmed:
        raise RuntimeError("请先在伺服和 GX28 页面完成零位设置")
    menu.say("单次注入会保持 CNC 当前坐标，只执行一遍：A 阀压丝、下针、换 B 阀、上针。")
    menu.say("请确认五根丝已装好、滑台在待机位；按回车执行，输入 Q 取消。")
    menu.ask("开始单次注入")
    plan = plan_row(row, geometry)
    coordinator = Coordinator(devices.servo, devices.cnc, devices.head, motion,
                              scan_z_mm=cfg.get("cnc3d", {}).get("scan_z_mm", 0.0),
                              operator_confirmed=True, z_datum=None,
                              valve_delay_s=cfg.get("cycle", {}).get("valve_delay_ms", 0) / 1000)
    result = coordinator.run_single(plan)
    if result.state is not RunState.COMPLETE:
        raise RuntimeError(result.error or "单次注入未完成")
    menu.say("单次注入完成。")


def run_manual_menu(*, store: ProfileStore, selection: dict[str, str | None],
                    config_path: Path, debug: bool = False, menu=None,
                    device_factory=None) -> int:
    """Run the component/manual menu and return a process-style exit code."""
    if menu is None:
        from master.menu import make_menu
        menu = make_menu()
    cfg = _load_config(store, selection, Path(config_path))
    geometry = MachineGeometry(5, 10.0, 10.0, float(cfg.get("servo", {}).get("lead_mm", 13.3333333)))
    row = _row_from_config(cfg)
    motion = _manual_motion(cfg, row)
    factory = _default_factory if device_factory is None else device_factory
    devices = factory(cfg=cfg, geometry=geometry, motion=motion, debug=debug)
    try:
        while True:
            menu.say("")
            menu.say("=== 组件手动控制 ===")
            menu.say("当前页面只会动作于你选中的组件；急停请使用设备急停或输入 !。")
            try:
                choice = menu.choose("选择组件", (
                    ("1", "CNC 三维模组"),
                    ("2", "GX28 滑台"),
                    ("3", "SV630N 伺服"),
                    ("4", "气阀 A/B"),
                    ("5", "单次注入（不移动 CNC）"),
                    ("0", "返回上一级菜单"),
                ))
            except WizardCancelled:
                return 0
            if choice == "0":
                return 0
            try:
                if choice == "1":
                    _cnc_page(menu, devices.cnc)
                elif choice == "2":
                    _head_page(menu, devices.head, motion)
                elif choice == "3":
                    _servo_page(menu, devices.servo, geometry)
                elif choice == "4":
                    _valve_page(menu, devices.head)
                elif choice == "5":
                    _single_injection(menu, devices, geometry, motion, row, cfg)
            except WizardCancelled:
                menu.say("已取消当前组件操作。")
            except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
                menu.warn(f"组件操作失败：{exc}")
    finally:
        try:
            if getattr(devices.head, "connected", False):
                for valve in ("a", "b"):
                    try:
                        devices.head.valve(valve, False)
                    except Exception:
                        pass
            if getattr(devices.servo, "connected", False):
                try:
                    devices.servo.disable()
                except Exception:
                    pass
        finally:
            for device in (devices.head, devices.cnc, devices.servo):
                try:
                    _close(device)
                except Exception:
                    pass
