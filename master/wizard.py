# -*- coding: utf-8 -*-
"""交互式开机向导：把"手改 config.toml + 记命令行参数"变成问答。

所有输入输出都走 Wizard 对象，所以无硬件、无 TTY 也能整套测。
本模块只负责采集与确认；设备的构造、连接、运行仍然复用 master.master 的
函数（惰性导入，避免循环依赖）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from master.devices import list_nics, list_serial_ports, rank_nics, rank_serial_ports
from master.model import MAX_ANGLE_DEG, MAX_DEPTH_MM, HeadMotion, RowSpec
from master.planner import highest_work_z_mm, plan_row

# 对应固件 insert_head.ino 的 WORK_MIN_MM，是机械行程不是工艺参数，不问操作员。
WORK_MIN_MM = -10.0
MIN_DIMENSION_MM = 0.001
DEFAULT_LIMIT_MM = 400.0


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
        的上限是金属接头的位置，报错必须把机械原因说出来，不然操作员只会再试一次。
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
    """采集本机环境（网卡、两个串口、CNC 软限位）。这些值会被持久化。"""
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
    cnc = existing.get("cnc3d", {})
    limits = {name: _ask_limit(w, f"CNC {axis} 软限位上限 mm", cnc.get(name))
              for name, axis in (("x_max", "X"), ("y_max", "Y"), ("z_max", "Z"))}
    return {
        "servo": {"adapter": adapter},
        "cnc3d": {"port": cnc_port, **limits},
        "head": {"port": head_port},
    }


def _reuse_or_choose(w: Wizard, label: str, current, choose):
    if current:
        w.say(f"{label}：{current}")
        if w.ask_yes_no("沿用这个值？", default=True):
            return current
    return choose()


def _ask_limit(w: Wizard, prompt: str, current) -> float:
    default = f"{float(current):g}" if current else f"{DEFAULT_LIMIT_MM:g}"
    return w.ask_float(prompt, minimum=1.0, default=default)


# ------------------------------------------------------------------ 作业
def collect_job(w: Wizard, geometry, *, defaults=None):
    """采集本次作业的植入参数，返回已通过排程的 (RowSpec, RowPlan)。"""
    defaults = defaults or {}
    w.say("=== 植入参数（每次作业都重新询问，不写入本机配置）===")
    w.say("针始终竖直，角度靠把管子架起来实现；角度带符号，正值 = 管面朝 +x 上升。")
    while True:
        angle = w.ask_float("架子角度 θ（度，带符号，例如 15 / -15 / 30）",
                            minimum=-MAX_ANGLE_DEG, maximum=MAX_ANGLE_DEG,
                            default=_default_text(defaults.get("angle_deg")), unit="°")
        length = w.ask_float("一排的强化区长度 mm（不是管料长度）", minimum=MIN_DIMENSION_MM,
                             default=_default_text(defaults.get("length_mm")))
        spacing = w.ask_float("点距 mm", minimum=MIN_DIMENSION_MM,
                              default=_default_text(defaults.get("spacing_mm")))
        depth = w.ask_float(
            f"植入深度 mm（沿垂直管面量，不超过 {MAX_DEPTH_MM:g}）",
            minimum=MIN_DIMENSION_MM, maximum=MAX_DEPTH_MM,
            default=_default_text(defaults.get("depth_mm")),
            too_big=f"必须不大于 {MAX_DEPTH_MM:g} mm——碳管壁只有 2 mm，"
                    f"再往下就是金属接头，这是碰撞不是废品")
        try:
            row = RowSpec(angle, length, spacing, depth)
        except ValueError as exc:
            w.warn(f"参数不成立：{exc}。请重新输入四个参数。")
            continue
        try:
            plan = plan_row(row, geometry)
        except ValueError as exc:
            w.warn(f"排程失败：{exc}。请重新输入四个参数。")
            continue
        travel = plan.placements[-1].cnc_x_mm - plan.placements[0].cnc_x_mm
        w.say(f"排程结果：{row.x_count} 个植入点，{len(plan.placements)} 次五针动作，"
              f"针距 {plan.needle_pitch_mm:g} mm，伺服目标 {plan.servo_target_mm:g} mm。")
        # 强化区长度和点距通常除不尽（样件规划里 20 mm @ 2.75 / 1.30 都不是整数倍）。
        # 排点数取能放下的那个整数，末端的余量不扎针——必须说出来，否则操作员会以为
        # 整段都扎满了，或者自己去做一遍除法才发现少了零点几毫米。
        tail = row.length_mm - row.covered_mm
        if tail > 1e-9:
            w.say(f"  强化区 {row.length_mm:g} mm，扎针覆盖 {row.covered_mm:g} mm"
                  f"（{row.x_count} × {row.spacing_mm:g}），末端 {tail:.4g} mm 不扎针"
                  f"（不足一个点距，放不下第 {row.x_count + 1} 个点）。")
        else:
            w.say(f"  强化区 {row.length_mm:g} mm，扎针覆盖 {row.covered_mm:g} mm，正好铺满。")
        # 角度的符号是针组的身份：+15 和 -15 的针长等差步长反号，物理上是两套针。
        w.say(f"滑台行程 {row.slide_travel_mm:.4g} mm（= 深度/cosθ）；"
              f"针长等差步长 {row.needle_length_step_mm(plan.needle_pitch_mm):+.4g} mm"
              f"（这套针组只配 {angle:+g}° 这个符号）；"
              f"排内 Z 变化 {row.slope * travel:+.3f} mm。")
        return row, plan


def _default_text(value):
    return None if value is None else f"{float(value):g}"


# ------------------------------------------------------------------ 开启确认
def confirm_zero_state(w: Wizard, servo, head, prepare_zeros) -> bool:
    """开机确认：零点不满足就转手动模式，满足则只需一句确认。

    注意断电重启后 connect() 会把伺服逻辑零点清空，所以正常一次开机必然
    要进手动模式——这一步不是走过场。
    """
    w.say("=== 开启确认 ===")
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


def wait_for_work_datum(w: Wizard, cnc) -> tuple[float, float]:
    """对刀：读回 **(x0, z0)** 两个坐标，不只是 Z。

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
        if w.ask_yes_no(f"把 X={position[0]:.3f} Z={position[2]:.3f} mm 记为对刀点？", default=True):
            return (position[0], position[2])
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


def check_limits(plan, limits) -> None:
    """排程落点是否落在 CNC 软限位内；不满足则抛 ValueError。"""
    x_max, y_max, _ = limits
    reach_x = max(placement.cnc_x_mm for placement in plan.placements)
    reach_y = max(placement.cnc_y_mm for placement in plan.placements)
    if reach_x > x_max or reach_y > y_max:
        raise ValueError(f"排程需要 X≥{reach_x:g}、Y≥{reach_y:g} mm，"
                         f"当前软限位只有 X={x_max:g}、Y={y_max:g} mm")


def confirm_assertions(w: Wizard) -> None:
    """开机前必须由操作员断言的三个事实。

    预检硬要求这三项为 true（`is not True` 检查），首次运行时本机配置里还没有，
    形成鸡生蛋。这里让操作员逐条确认后写入；填错了也兜得住——固件协议 2 的
    握手在 connect() 时仍然会真实校验，不是空头承诺。
    """
    w.say("=== 最后确认（以下三项会写入本机配置，必须先在实机上核实）===")
    w.say("1. ESP32 已烧录本仓库的协议 2 固件（连接时会真实握手，填错会在连接时报错）。")
    w.say("2. 滑台可用下行窗口已在实机测过；本次滑台行程由 depth/cosθ 推导（见排程回显）。")
    w.say("3. 上阀 B 退针后释放、切丝与供丝流程已确认。")
    while True:
        if not w.ask("确认以上三项都已在实机核实，请按回车继续"):
            return
        w.warn("请按回车确认；输入 Q 可取消本次设置。")


# ------------------------------------------------------------------ 编排
def run_setup_wizard(config_path, job_overrides=None, *, wizard=None, find_adapters=None,
                     list_ports=None, results_dir=None, input_fn=None,
                     output=None, debug=False) -> int:
    """交互式实机流程：环境 → 作业 → 连接 → 零点 → 对刀 → 确认 → 运行。"""
    from master import master as cli
    from master.local_config import load_merged, local_path_for, write_local

    config_path = Path(config_path)
    if not config_path.exists():
        raise ValueError(f"配置：无法读取 {config_path}：模板不存在")
    w = Wizard(input_fn=input_fn, output=output) if wizard is None else wizard
    local_path = local_path_for(config_path)
    geometry = cli._known_geometry()
    cfg = load_merged(config_path)
    devices = None
    try:
        env = collect_environment(w, cfg, find_adapters=find_adapters,
                                  list_ports=list_ports)
        row, plan = collect_job(w, geometry, defaults=cfg.get("job", {}))
        env = _reconcile_limits(w, env, plan)
        motion = _build_motion(row)
        # 先落盘一次：即使后面在零点或对刀处中止，操作员选的端口/网卡也留下来了。
        write_local(local_path, _local_sections(env, motion))
        w.say(f"已写入 {local_path}（此时还没有 scan_z_mm）")

        _apply(cfg, env)
        cfg.setdefault("head", {}).update(_head_window(motion))
        echo = cli._make_echo(debug)
        devices = cli._open_live_devices(cfg, geometry, motion, echo=echo)
        cli._connect_live_devices(devices)
        w.say(f"连接后状态：CNC WPos={devices.cnc.position()}，"
              f"滑台软件读数={devices.head.position_mm():.3f} mm。")
        if not confirm_zero_state(w, devices.servo, devices.head, cli._prepare_live_zeros):
            w.warn("未完成零点确认，未开始自动植入。")
            return 1
        datum = wait_for_work_datum(w, devices.cnc)
        scan_z = wait_for_safe_height(w, devices.cnc, datum, plan)
        confirm_assertions(w)
        write_local(local_path, _local_sections(env, motion, scan_z))
        w.say(f"已写入 {local_path}（含 scan_z_mm={scan_z:g} mm）")

        # 用写出的文件重新校验一遍：写出器的 bug 必须在这里暴露，而不是在运动中途。
        cfg2, row2, _, motion2, scan_z2, plan2, datum2 = cli._read_live_configuration(
            config_path, True, {**job_values(row), **(job_overrides or {})}, datum=datum)
        _check_consistency(cfg, cfg2)
        w.say(f"最终参数：{row2.x_count} 个植入点，{len(plan2.placements)} 次五针动作，"
              f"针距 {plan2.needle_pitch_mm:g} mm 伺服 {plan2.servo_target_mm:g} mm，"
              f"对刀点 X={datum2[0]:g} Z={datum2[1]:g} mm，安全高度 Z={scan_z2:g} mm。")
        if not w.ask_yes_no("开始自动植入？", default=True):
            w.warn("操作员未放行，未开始自动植入。")
            return 1
        return cli._execute_live_job(cfg2, row2, motion2, scan_z2, plan2, devices,
                                     datum=datum2,
                                     results_dir=_results_dir(config_path, results_dir),
                                     echo=echo)
    except WizardCancelled as exc:
        w.warn(f"已取消（{exc}），未开始自动植入。")
        return 1
    finally:
        if devices is not None:
            cli._close_live_devices(devices)


def _apply(cfg: dict, env: dict) -> None:
    for section, values in env.items():
        cfg.setdefault(section, {}).update(values)


def _reconcile_limits(w: Wizard, env: dict, plan) -> dict:
    limits = (env["cnc3d"]["x_max"], env["cnc3d"]["y_max"], env["cnc3d"]["z_max"])
    while True:
        try:
            check_limits(plan, limits)
            return env
        except ValueError as exc:
            w.warn(f"{exc}。请重新输入 CNC 软限位。")
            x_max = w.ask_float("CNC X 软限位上限 mm", minimum=1.0, default=f"{limits[0]:g}")
            y_max = w.ask_float("CNC Y 软限位上限 mm", minimum=1.0, default=f"{limits[1]:g}")
            env["cnc3d"].update(x_max=x_max, y_max=y_max)
            limits = (x_max, y_max, limits[2])


def _build_motion(row: RowSpec) -> HeadMotion:
    """下行目标由深度推导：深度沿垂直管面量，滑台竖直行程 = depth/cosθ。"""
    return HeadMotion(standby_mm=0.0, insertion_mm=-row.slide_travel_mm,
                      work_min_mm=WORK_MIN_MM, work_max_mm=0.0, datum_confirmed=True)


def _head_window(motion: HeadMotion) -> dict:
    return {"firmware_verified": True, "datum_confirmed": True,
            "standby_mm": motion.standby_mm, "insertion_mm": motion.insertion_mm,
            "work_min_mm": motion.work_min_mm, "work_max_mm": motion.work_max_mm}


def _local_sections(env: dict, motion: HeadMotion, scan_z_mm: float | None = None) -> dict:
    """本机配置里到底写哪些键，只在这里决定。

    [job] 不写：角度与排长是每次作业的参数，不是本机环境。
    """
    cnc3d = dict(env["cnc3d"])
    if scan_z_mm is not None:
        cnc3d["scan_z_mm"] = scan_z_mm
    return {
        "servo": {"adapter": env["servo"]["adapter"]},
        "cnc3d": cnc3d,
        "head": {"port": env["head"]["port"], **_head_window(motion)},
        "cycle": {"upper_release_after_retract": True},
    }


def job_values(row: RowSpec) -> dict:
    return {"angle_deg": row.angle_deg, "length_mm": row.length_mm,
            "spacing_mm": row.spacing_mm, "depth_mm": row.depth_mm}


def _check_consistency(cfg_built: dict, cfg_validated: dict) -> None:
    """写出的配置必须和构造设备时用的配置一致。

    否则跑的就不是刚对好刀的那台机器——写出器的 bug 必须在这里暴露。
    """
    for section, key in (("servo", "adapter"), ("cnc3d", "port"), ("head", "port")):
        if cfg_built[section][key] != cfg_validated[section][key]:
            raise RuntimeError(
                f"写出的配置 [{section}] {key} 与已连接设备不一致，已中止")


def _results_dir(config_path, results_dir):
    return Path(results_dir) if results_dir is not None else Path(config_path).parent / "runs"
