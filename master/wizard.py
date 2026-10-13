# -*- coding: utf-8 -*-
"""交互式开机向导：把"手改 config.toml + 记命令行参数"变成问答。

所有输入输出都走 Wizard 对象，所以无硬件、无 TTY 也能整套测。
本模块只负责采集与确认；设备的构造、连接、运行仍然复用 master.master 的
函数（惰性导入，避免循环依赖）。
"""
from __future__ import annotations

import math
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from master.devices import list_nics, list_serial_ports, rank_nics, rank_serial_ports
from master.model import DEFAULT_CUT_CLEARANCE_MM, HEAD_PRE_DESCENT_MM, HEAD_WORK_MIN_MM, HEAD_WORK_MAX_MM, MAX_CUT_CLEARANCE_MM, PRESET_ANGLES, PRESET_COUNTS, HeadMotion, PresetSpec, validate_cut_clearance_mm
from master.planner import highest_work_z_mm, plan_preset_row

# 对应固件 insert_head.ino 的下行软件限位；实际机械余量仍须人工确认。
WORK_MIN_MM = HEAD_WORK_MIN_MM
MIN_DIMENSION_MM = 0.001


class WizardCancelled(Exception):
    """操作员中止了向导（输入 Q、EOF 或 Ctrl-C）。"""


class Wizard:
    def __init__(self, *, input_fn=None, output=None,
                 cancel_words=("Q", "QUIT", "取消")):
        # input_fn 留空时在构造这一刻才取内建 input，而不是在 def 那一刻绑定：
        # 测试用 patch("builtins.input") 换掉的正是这个内建。
        self._input = input if input_fn is None else input_fn
        self.output = sys.stdout if output is None else output
        self._cancel_words = cancel_words

    def say(self, message: str = "") -> None:
        print(message, file=self.output)

    def warn(self, message: str) -> None:
        print(f"  ! {message}", file=self.output)

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        try:
            answer = self._input(f"{prompt}{suffix} > ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise WizardCancelled("输入已关闭") from exc
        if answer.upper() in self._cancel_words:
            raise WizardCancelled("操作员取消")
        if not answer and default is not None:
            return default
        return answer

    def _ask_validated(self, prompt, parse, *, default=None, attempts: int = 20):
        for _ in range(attempts):
            raw = self.ask(prompt, default=default)
            try:
                return parse(raw)
            except ValueError as exc:
                self.warn(str(exc))
        raise WizardCancelled(f"连续 {attempts} 次输入无效，已中止")

    def ask_float(self, prompt: str, *, minimum: float | None = None,
                  maximum: float | None = None, default: str | None = None,
                  unit: str = "mm", too_big: str | None = None) -> float:
        """``too_big`` 覆盖上限报错文案。工艺上限不只是"数字大了"——比如植入深度
        的上限与实际滑台工作窗口分别检查，提示应说明当前输入限制。
        """
        def parse(raw: str) -> float:
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

        return self._ask_validated(prompt, parse, default=default)

    def ask_yes_no(self, prompt: str, *, default: bool = True) -> bool:
        def parse(raw: str) -> bool:
            folded = raw.strip().lower()
            if folded in ("y", "yes", "是", "1"):
                return True
            if folded in ("n", "no", "否", "0"):
                return False
            raise ValueError(f"请输入 y 或 n，收到 {raw!r}")

        return self._ask_validated(prompt, parse, default="Y" if default else "N")

    def choose(self, prompt: str, options, *, default: str | None = None) -> str:
        """把选项渲染成编号菜单，返回所选项的 key。

        options 形如 (("1", "实机运行"), ("0", "退出"))——编号由调用方显式给出，
        于是"退出"可以是 [0]，不必排到最后变成 [5]。key 原样返回，调用方按字符串比。
        """
        keys = [key for key, _ in options]
        for key, label in options:
            self.say(f"  [{key}] {label}")
        for _ in range(20):
            raw = self.ask(prompt, default=default)
            if raw in keys:
                return raw
            self.warn(f"无效编号：{raw}（可用 {'/'.join(keys)}）")
        raise WizardCancelled("连续 20 次输入无效，已中止")


# ------------------------------------------------------------------ 串口
def select_serial_port(w: Wizard, *, role: str, exclude=(), list_ports=None) -> str:
    """选一个串口。exclude 里的端口会被拒绝，防止两路抢同一个口。"""
    while True:
        ports = rank_serial_ports(list_serial_ports(list_ports))
        candidates = [port for port in ports if port.reject_reason is None]
        w.say(f"--- {role} ---")
        if candidates:
            for number, port in enumerate(candidates, 1):
                w.say(f"  [{number}] {port.device}  {port.description}")
        else:
            w.say("  没有检测到 USB 串口。请插好 USB 线、给设备上电，然后重新扫描。")
        command = w.ask("编号 / R 重新扫描 / A 显示全部串口 / M 手动输入端口名").upper()
        if command == "R":
            continue
        if command == "A":
            device = _choose_serial_from_all(w, ports)
        elif command == "M":
            device = w.ask("端口名（例如 COM9）").upper()
        else:
            chosen = _pick_by_number(w, command, candidates)
            device = chosen.device if chosen else None
        if device is None:
            continue
        if device in exclude:
            w.warn(f"{device} 已被另一路占用，请另选一个端口。")
            continue
        return device


def _choose_serial_from_all(w: Wizard, ports) -> str | None:
    w.say("全部串口（含被过滤的）：")
    for number, port in enumerate(ports, 1):
        reason = f"  ← {port.reject_reason}" if port.reject_reason else ""
        w.say(f"  [{number}] {port.device}  {port.description}{reason}")
    chosen = _pick_by_number(w, w.ask("编号").upper(), ports)
    return chosen.device if chosen else None


# ------------------------------------------------------------------ 网卡
def select_nic(w: Wizard, *, find_adapters=None) -> str:
    """选 EtherCAT 网卡，返回人类可读描述（不是 pysoem 设备名）。"""
    while True:
        nics = rank_nics(list_nics(find_adapters))
        candidates = [nic for nic in nics if nic.reject_reason is None]
        w.say("--- EtherCAT 网卡 ---")
        if len(candidates) == 1:
            w.say(f"  检测到有线网卡：{candidates[0].description}")
            if w.ask_yes_no("使用这块网卡？", default=True):
                return candidates[0].description
        elif candidates:
            for number, nic in enumerate(candidates, 1):
                w.say(f"  [{number}] {nic.description}")
            command = w.ask("编号 / A 显示全部网卡 / R 重新扫描").upper()
            if command == "R":
                continue
            if command == "A":
                chosen = _choose_nic_from_all(w, nics)
                if chosen is not None:
                    return chosen.description
                continue
            chosen = _pick_by_number(w, command, candidates)
            if chosen is not None:
                return chosen.description
            continue
        else:
            w.say("  没有检测到可用的有线网卡。")
        chosen = _choose_nic_from_all(w, nics)
        if chosen is not None:
            return chosen.description


def _choose_nic_from_all(w: Wizard, nics):
    w.say("全部网卡（含被过滤的）：")
    for number, nic in enumerate(nics, 1):
        reason = f"  ← {nic.reject_reason}" if nic.reject_reason else ""
        w.say(f"  [{number}] {nic.description}{reason}")
    chosen = _pick_by_number(w, w.ask("编号").upper(), nics)
    if chosen is None:
        return None
    if chosen.reject_reason:
        # 过滤只是提示：USB 网卡转 EtherCAT 会被误判，所以留这条出口，但要讲清后果。
        w.warn(f"{chosen.description} 属于「{chosen.reject_reason}」，EtherCAT 很可能无法工作。")
        if not w.ask_yes_no("仍然使用它？", default=False):
            return None
    return chosen


def _pick_by_number(w: Wizard, text: str, items):
    if text.isdigit() and 1 <= int(text) <= len(items):
        return items[int(text) - 1]
    w.warn(f"无效编号：{text}")
    return None


# ------------------------------------------------------------------ 环境
def collect_environment(w: Wizard, existing, *, find_adapters=None, list_ports=None) -> dict:
    """采集本机环境（网卡、两个串口）。这些值会被持久化。"""
    w.say("=== 环境配置（保存到 config.local.toml，下次运行作为默认值）===")
    adapter = _reuse_or_choose(
        w, "EtherCAT 网卡", existing.get("servo", {}).get("adapter"),
        lambda: select_nic(w, find_adapters=find_adapters))
    cnc_port = _reuse_or_choose(
        w, "CNC 三维模组串口", existing.get("cnc3d", {}).get("port"),
        lambda: select_serial_port(w, role="CNC 三维模组", list_ports=list_ports))
    head_port = _reuse_or_choose(
        w, "GX28 植入滑台串口", existing.get("head", {}).get("port"),
        lambda: select_serial_port(w, role="GX28 植入滑台",
                                   exclude=(cnc_port,), list_ports=list_ports))
    return {
        "servo": {"adapter": adapter},
        "cnc3d": {"port": cnc_port},
        "head": {"port": head_port},
    }


def _reuse_or_choose(w: Wizard, label: str, current, choose):
    if current:
        w.say(f"{label}：{current}")
        if w.ask_yes_no("沿用这个值？", default=True):
            return current
    return choose()


# ------------------------------------------------------------------ 作业
def collect_preset(w: Wizard, geometry, *, defaults=None):
    """Collect density, supported fixture angle and perpendicular depth each run."""
    from master.master import _print_plan
    defaults = defaults or {}
    w.say("=== 预设任务：20 mm 强化区，仅装最右两针，针距固定 10 mm，伺服保持零位 ===")
    w.say("对刀以两根已装针中左针为排头；角度正值表示管面朝 +X 上升。")
    while True:
        count = int(w.choose("每排植入根数", tuple((str(n), f"{n} 根") for n in PRESET_COUNTS),
                             default=str(defaults.get("count", 7)) if defaults.get("count", 7) in PRESET_COUNTS else "7"))
        default_angle = defaults.get("angle_deg", 15)
        angle = float(w.choose("架子角度 θ", tuple((str(a), f"{a}°") for a in PRESET_ANGLES),
                               default=_default_text(default_angle) if default_angle in PRESET_ANGLES else "15"))
        depth = w.ask_float("植入深度 mm（垂直管面；实际行程须在滑台工作窗口内）",
                            minimum=MIN_DIMENSION_MM,
                            default=_default_text(defaults.get("depth_mm")))
        try:
            row = PresetSpec(angle, count, depth)
            plan = plan_preset_row(row, geometry)
        except ValueError as exc:
            w.warn(f"参数不成立：{exc}。请重新输入。")
            continue
        # Keep terminal and GUI logs on their respective output sink.
        _print_plan(row, plan, emit=w.say)
        w.say("动作 X：" + ", ".join(f"{p.cnc_x_mm:.4g}" for p in plan.placements) + " mm")
        w.say("区内落点：" + ", ".join(f"{i * row.spacing_mm:.4g}" for i in range(row.count)) + " mm")
        return row, plan


def _default_text(value):
    return None if value is None else f"{float(value):g}"


# ------------------------------------------------------------------ 开启确认
def confirm_zero_state(w: Wizard, servo, head, prepare_zeros, *, servo_required=True) -> bool:
    """开机确认：零点不满足就转手动模式，满足则只需一句确认。

    注意断电重启后 connect() 会把伺服逻辑零点清空，所以正常一次开机必然
    要进手动模式——这一步不是走过场。
    """
    w.say("=== 开启确认 ===")
    if not servo_required:
        w.say(f"滑台当前软件读数 {head.position_mm():.3f} mm（待机应为 0 mm）。")
        if not servo.connected or servo.origin_count is None:
            w.warn("伺服未连接或尚未设零，未自动调整针距；请人工核对当前机械针距。")
        if head.zero_confirmed:
            return w.ask_yes_no("滑台仍在已确认的待机零点位置？", default=True)
        w.warn("滑台还没有确认待机软件零位——进入手动控制模式。")
        return prepare_zeros(servo, head)
    w.say(f"滑台当前软件读数 {head.position_mm():.3f} mm（待机应为 0 mm）；"
          f"伺服相对本次连接初始读数 {servo.setup_offset_mm:+.3f} mm（不是机械基准）。")
    if servo.origin_count is not None and head.zero_confirmed:
        w.say("伺服逻辑零点和滑台软件零位本次连接内都已确认。")
        return w.ask_yes_no("滑台与伺服都仍在零点位置？", default=True)
    if servo.origin_count is None:
        w.warn("伺服本次连接还没有设逻辑零点——必须进入手动模式回零。")
    if not head.zero_confirmed:
        w.warn("滑台还没有确认待机软件零位——必须进入手动模式。")
    w.say("进入手动控制模式。")
    return prepare_zeros(servo, head)


def wait_for_work_datum(w: Wizard, cnc, *, include_y=False) -> tuple:
    """Read touch-off coordinates; live operation retains Y as well as X/Z.

    针尖连线与管面平行，所以排内 Z 必须跟着 X 走：`z(x) = z0 + (x-x0)·tanθ`。
    X 也是必需的一半——只记 Z 就无从知道从哪儿开始算斜率。
    """
    w.say("=== 对刀（工作高度）===")
    w.say("请手动把 X 移到排头，再把 Z 降到针尖刚好接触管顶母线；对完后回到这里。")
    while True:
        w.ask("对刀完成后按 Enter 继续")
        position = cnc.position()
        if position is None:
            w.warn("读不到 CNC 工作坐标（WPos）。请检查 USB 连接和控制器状态后重试。")
            continue
        w.say(f"当前 CNC 工作坐标：X={position[0]:.3f} Y={position[1]:.3f} Z={position[2]:.3f} mm")
        if w.ask_yes_no(f"把 X={position[0]:.3f} Y={position[1]:.3f} Z={position[2]:.3f} mm 记为对刀点？", default=True):
            return tuple(position) if include_y else (position[0], position[2])
        w.say("好的，请重新对刀。")


def wait_for_safe_height(w: Wizard, cnc, datum, plan) -> float:
    """排间安全高度：够把管子转一个周向位置。

    必须高过排内**最高**的工作点。θ 带符号——正角时最高点在排尾、负角时在排头——
    所以这里拿 `highest_work_z_mm` 取全排最大，绝不假定单调。
    """
    highest = highest_work_z_mm(plan, datum)
    side = "排尾" if plan.slope >= 0 else "排头"
    w.say("=== 安全高度（排间抬刀高度）===")
    w.say(f"请把 Z 抬到够把管子转过一个周向位置的高度。排内最高工作点是 "
          f"{highest:+.3f} mm，在{side}；安全高度不得低于它。")
    while True:
        w.ask("抬到位后按 Enter 继续")
        position = cnc.position()
        if position is None:
            w.warn("读不到 CNC 工作坐标（WPos）。请检查 USB 连接和控制器状态后重试。")
            continue
        w.say(f"当前 CNC Z={position[2]:.3f} mm。")
        if position[2] + 1e-9 < highest:
            w.warn(f"这个高度低于排内最高工作点 {highest:+.3f} mm。照这个高度抬刀，"
                   f"{side}会往管子里扎下去。请继续抬高。")
            continue
        if w.ask_yes_no(f"把 Z={position[2]:.3f} mm 记为排间安全高度？", default=True):
            return position[2]


def prepare_head_for_touch_off(w: Wizard, head, motion: HeadMotion) -> bool:
    """Reserve cutting travel without changing the confirmed software zero."""
    w.say("=== GX28 对刀准备 ===")
    w.say(f"先保持针尖离开工件，确认滑台从当前零点向下 {HEAD_PRE_DESCENT_MM:g} mm 无障碍、金属丝未被夹紧。"
          f"程序将滑台移到 {-HEAD_PRE_DESCENT_MM:g} mm，再进行 CNC 对刀；不会重新设置滑台零点。")
    if not w.ask_yes_no(f"确认下方有 {HEAD_PRE_DESCENT_MM:g} mm 余量，允许 GX28 向下准备？", default=False):
        return False
    initial = head.position_mm()
    if not head.zero_confirmed or not math.isfinite(initial) or abs(initial) > 0.05:
        raise RuntimeError("GX28 对刀准备前必须在本次人工确认的 0 mm 位置；请重新核实零点")
    verify = getattr(head, "verify_work_window", None)
    if verify is not None:
        verify(motion)
    head.valve("a", False)
    head.valve("b", False)
    head.motion = motion
    head.move_mm(motion.standby_mm)
    position = head.position_mm()
    if not math.isfinite(position) or abs(position-motion.standby_mm) > 0.05:
        raise RuntimeError("GX28 对刀准备位置回读不一致，未开始对刀")
    w.say(f"GX28 已到 {position:g} mm；请在此位置进行 CNC 对刀。")
    return True


def lift_head_after_touch_off(w: Wizard, head, motion: HeadMotion) -> tuple[HeadMotion, float]:
    position = head.position_mm()
    if not head.zero_confirmed or not math.isfinite(position) or abs(position-motion.standby_mm) > 0.05:
        raise RuntimeError("GX28 对刀时的位置已改变；请重新准备并对刀")
    # Preserve the measured datum and use ordinary absolute position commands.
    distance = w.ask_float("对刀已记录。GX28 可动针头上升多少 mm？（0关闭剪丝抬升）",
                           minimum=0, maximum=min(MAX_CUT_CLEARANCE_MM, motion.work_max_mm-position),
                           default=f"{min(motion.cut_clearance_mm, motion.work_max_mm-position):g}")
    lifted = replace(motion, standby_mm=position,
                     insertion_mm=position+motion.down_delta_mm,
                     cut_clearance_mm=distance)
    head.motion = lifted
    head.move_mm(lifted.cut_position_mm)
    readback = head.position_mm()
    if not math.isfinite(readback) or abs(readback-lifted.cut_position_mm) > 0.05:
        raise RuntimeError("GX28 上升后位置回读不一致，未记录安全位置")
    w.say(f"已记录 GX28 对刀位 {position:g} mm、上升 {distance:g} mm、剪丝安全位置 {readback:g} mm。"
          "CNC 到植入位置后才下降回对刀位；此位置与 CNC 排间 Z 安全高度分别记录。")
    return lifted, readback


def confirm_assertions(w: Wizard) -> None:
    """开机前必须由操作员断言的三个事实。

    预检硬要求这三项为 true（`is not True` 检查），首次运行时本机配置里还没有，
    形成鸡生蛋。这里让操作员逐条确认后写入；填错了也兜得住——固件协议 2 的
    握手在 connect() 时仍然会真实校验，不是空头承诺。
    """
    w.say("=== 最后确认（以下三项会写入本机配置，必须先在实机上核实）===")
    w.say("1. ESP32 已烧录本仓库的协议 2 固件（连接时会真实握手，填错会在连接时报错）。")
    w.say("2. 滑台可用下行窗口已在实机测过；本次滑台行程由 depth/cosθ 推导（见排程回显）。")
    w.say("3. 剪丝与供丝流程已确认；启用剪丝抬升时，上阀 B 保持夹紧直到剪丝确认。")
    while True:
        if not w.ask("确认以上三项都已在实机核实，请按回车继续"):
            return
        w.warn("请按回车确认；输入 Q 可取消本次设置。")


# ------------------------------------------------------------------ 编排
def run_setup_wizard(config_path, job_overrides=None, *, wizard=None, find_adapters=None,
                     list_ports=None, results_dir=None, input_fn=None,
                     output=None, debug=False, profile_store=None,
                     profile_selection=None, menu=None, command_reader=None, job_source=None,
                     prepared_config=None) -> int:
    """交互式实机流程：共用接口配置 → 作业 → 连接 → 零点 → 对刀 → 确认 → 运行。

    `menu` 与 `command_reader` 是给图形界面的两个注入点：安全设置菜单和急停指令
    在这里各有一次**绕过 `wizard` 参数**的直接调用（下面 `_prepare_live_zeros` 与
    `_execute_live_job` 两处），留空即原来的终端行为。
    """
    from master import master as cli
    from master.local_config import load_merged, local_path_for, write_local

    config_path = Path(config_path)
    if not config_path.exists():
        raise ValueError(f"配置：无法读取 {config_path}：模板不存在")
    w = Wizard(input_fn=input_fn, output=output) if wizard is None else wizard
    local_path = local_path_for(config_path)
    geometry = cli._known_geometry()
    source = job_source or cli.PresetJobSource()
    devices = None
    try:
        if prepared_config is None:
            from master.interfaces import prepare_interfaces
            cfg = prepare_interfaces(config_path, w, store=profile_store,
                                     selection=profile_selection, find_adapters=find_adapters,
                                     list_ports=list_ports)
        else:
            cfg = deepcopy(prepared_config)
        env = {section: dict(cfg.get(section, {})) for section in ("servo", "cnc3d", "head")}
        row, _ = collect_preset(w, geometry, defaults={**cfg.get("job", {}), **(job_overrides or {})})
        row = source.build(cfg, {**preset_values(row), **(job_overrides or {})})
        plan = source.plan(row, geometry)
        clearance = cfg.get("cycle", {}).get("cut_clearance_mm", DEFAULT_CUT_CLEARANCE_MM)
        # Earlier preparations reserved only 5 or 10 mm. Offer the new default
        # when redoing those setups; retain choices from a 15 mm preparation.
        if cfg.get("head", {}).get("standby_mm") != -HEAD_PRE_DESCENT_MM:
            clearance = DEFAULT_CUT_CLEARANCE_MM
        motion = _build_motion(row, clearance)
        # 先落盘一次：即使后面在零点或对刀处中止，操作员选的端口/网卡也留下来了。
        _apply(cfg, env)
        # Keep measured coordinates and confirmation flags untouched until
        # touch-off, cutting clearance and the final assertions are complete.
        cfg["job"] = preset_values(row)
        write_local(local_path, cfg)
        w.say(f"已保存接口与作业参数到 {local_path}（滑台窗口、对刀位与安全高度待本次实机确认）")
        echo = cli._make_echo(debug)
        devices = cli._open_live_devices(cfg, geometry, motion, echo=echo)
        cli._connect_live_devices(devices)
        w.say(f"连接后状态：CNC WPos={devices.cnc.position()}，"
              f"滑台软件读数={devices.head.position_mm():.3f} mm。")
        w.say(f"GX28 的初始 0 mm 是本次人工确认的位置，随后准备到 {-HEAD_PRE_DESCENT_MM:g} mm 才对刀。"
              "再次上电时仍须人工核实初始零点，不能把上次对刀位直接当作它。")
        if not confirm_zero_state(w, devices.servo, devices.head,
                                  lambda servo, head: cli._prepare_live_zeros(
                                      servo, head, menu=menu or w, servo_required=False),
                                  servo_required=False):
            w.warn("未完成零点确认，未开始自动植入。")
            return 1
        if not prepare_head_for_touch_off(w, devices.head, motion):
            w.warn("未允许 GX28 对刀准备，未移动针头或开始自动植入。")
            return 1
        touch_off = wait_for_work_datum(w, devices.cnc, include_y=True)
        datum = (touch_off[0], touch_off[2])
        row_y = touch_off[1]
        motion, cut_safe = lift_head_after_touch_off(w, devices.head, motion)
        scan_z = wait_for_safe_height(w, devices.cnc, datum, plan)
        confirm_assertions(w)
        _apply(cfg, _local_sections(env, motion, scan_z))
        cfg["head"]["cut_safe_mm"] = cut_safe
        write_local(local_path, cfg)
        w.say(f"已写入 {local_path}（含 scan_z_mm={scan_z:g} mm）")
        if profile_store is not None and profile_selection:
            machine = profile_selection.get("machine")
            if machine:
                profile_store.save("machines", machine,
                                   {section: values for section, values in cfg.items() if section != "custom"})
                w.say(f"已同步保存机器配置 {machine}。")

        # 用写出的文件重新校验一遍：写出器的 bug 必须在这里暴露，而不是在运动中途。
        cfg2, row2, _, motion2, scan_z2, plan2, datum2 = cli._read_live_configuration(
            config_path, True, preset_values(row), datum=datum, job_source=source,
            row_y_mm=row_y)
        _check_consistency(cfg, cfg2)
        w.say(f"最终参数：{row2.x_count} 个植入点，{len(plan2.placements)} 次扎针动作（每次两针），"
              f"针距 {plan2.needle_pitch_mm:g} mm 伺服 {plan2.servo_target_mm:g} mm，"
              f"对刀点 X={datum2[0]:g} Y={row_y:g} Z={datum2[1]:g} mm，安全高度 Z={scan_z2:g} mm，"
              f"剪丝额外抬升 {motion2.cut_clearance_mm:g} mm。")
        if not w.ask_yes_no("开始自动植入？", default=True):
            w.warn("操作员未放行，未开始自动植入。")
            return 1
        return cli._execute_live_job(cfg2, row2, motion2, scan_z2, plan2, devices,
                                     datum=datum2, row_y_mm=row_y,
                                     results_dir=_results_dir(config_path, results_dir),
                                     echo=echo, command_reader=command_reader)
    except WizardCancelled as exc:
        w.warn(f"已取消（{exc}），未开始自动植入。")
        return 1
    finally:
        if devices is not None:
            cli._close_live_devices(devices)


def _apply(cfg: dict, env: dict) -> None:
    for section, values in env.items():
        cfg.setdefault(section, {}).update(values)


def _build_motion(row: PresetSpec, cut_clearance_mm=DEFAULT_CUT_CLEARANCE_MM) -> HeadMotion:
    """下行目标由深度推导：深度沿垂直管面量，滑台竖直行程 = depth/cosθ。"""
    clearance = validate_cut_clearance_mm(cut_clearance_mm)
    standby = -HEAD_PRE_DESCENT_MM
    if standby-row.slide_travel_mm < WORK_MIN_MM:
        raise ValueError(f"滑台行程超出当前工作窗口：{row.angle_deg:g}°、深度 {row.depth_mm:g} mm "
                         f"需要下行 {row.slide_travel_mm:.3f} mm；从对刀位 {standby:g} mm "
                         f"最多可下行 {standby-WORK_MIN_MM:g} mm。未连接设备，请减小深度。")
    return HeadMotion(standby_mm=standby, insertion_mm=standby-row.slide_travel_mm,
                      work_min_mm=WORK_MIN_MM, work_max_mm=HEAD_WORK_MAX_MM, datum_confirmed=True,
                      cut_clearance_mm=clearance)


def _head_window(motion: HeadMotion) -> dict:
    return {"firmware_verified": True, "datum_confirmed": True,
            "standby_mm": motion.standby_mm, "insertion_mm": motion.insertion_mm,
            "work_min_mm": motion.work_min_mm, "work_max_mm": motion.work_max_mm,
            "cut_safe_mm": motion.cut_position_mm}


def _local_sections(env: dict, motion: HeadMotion, scan_z_mm: float | None = None) -> dict:
    """本机配置里到底写哪些键，只在这里决定。

    [job] 不写：角度与排长是每次作业的参数，不是本机环境。
    """
    cnc3d = dict(env["cnc3d"])
    if scan_z_mm is not None:
        cnc3d["scan_z_mm"] = scan_z_mm
    return {
        "servo": {"adapter": env["servo"].get("adapter", "")},
        "cnc3d": cnc3d,
        "head": {"port": env["head"]["port"], **_head_window(motion)},
        "cycle": {"upper_release_after_retract": True, "cut_clearance_mm": motion.cut_clearance_mm},
    }


def preset_values(row: PresetSpec) -> dict:
    return {"angle_deg": row.angle_deg, "count": row.count, "depth_mm": row.depth_mm}


def _check_consistency(cfg_built: dict, cfg_validated: dict) -> None:
    """写出的配置必须和构造设备时用的配置一致。

    否则跑的就不是刚对好刀的那台机器——写出器的 bug 必须在这里暴露。
    """
    for section, key in (("servo", "adapter"), ("cnc3d", "port"), ("head", "port")):
        if cfg_built.get(section, {}).get(key, "") != cfg_validated.get(section, {}).get(key, ""):
            raise RuntimeError(
                f"写出的配置 [{section}] {key} 与已连接设备不一致，已中止")


def _results_dir(config_path, results_dir):
    return Path(results_dir) if results_dir is not None else Path(config_path).parent / "runs"
