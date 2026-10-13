"""Z-pin CLI. Simulation and live operation share one planner and coordinator."""

from __future__ import annotations

import argparse
import math
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from master.coordinator import Coordinator, RunState
from master.local_config import load_merged, local_path_for, write_local
from master.model import HEAD_WORK_MIN_MM, HEAD_WORK_MAX_MM, PRESET_ANGLES, HeadMotion, MachineGeometry, PresetSpec, validate_cut_clearance_mm
from master.operator_gate import OperatorGate
from master.planner import highest_work_z_mm, plan_at_x, plan_at_y, plan_preset_row, work_z_mm
from master.simulated import SimulationRig


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.toml"
    return Path(__file__).resolve().parent / "config.toml"


JOB_KEYS = ("angle_deg", "count", "depth_mm")

# --smoke-test 的新基准：0.2% 密度的一排，重复三遍，好让打包验收真的走到排循环。
SMOKE_JOB = {"angle_deg": 15.0, "count": 7, "depth_mm": 2.0}
SMOKE_ROWS = 3
SMOKE_DATUM = (0.0, -5.0)
SMOKE_SCAN_Z_MM = 0.0


class PresetJobSource:
    """Shared construction and planning for collection and final live preflight."""

    def build(self, cfg, overrides):
        unknown = set(overrides) - set(JOB_KEYS)
        if unknown:
            raise ValueError(f"配置：未知的作业覆盖项 {', '.join(sorted(unknown))}")
        values = {key: overrides.get(key, cfg.get("job", {}).get(key)) for key in JOB_KEYS}
        for key, value in values.items():
            if value is None or value == "":
                raise ValueError(f"配置：[job] {key} 必填")
        return PresetSpec(**values)

    def plan(self, job, machine):
        return plan_preset_row(job, machine)


def _known_geometry() -> MachineGeometry:
    return MachineGeometry(station_count=5, station_width_mm=10,
                           max_expansion_mm=10, servo_lead_mm=13.3333333)


def _print_plan(row: PresetSpec, plan, *, datum=None, scan_z_mm: float | None = None, emit=print) -> None:
    step = row.needle_length_step_mm(plan.needle_pitch_mm)
    emit(f"作业：架角 {row.angle_deg:+g}°，强化区 {row.length_mm:g} mm，"
          f"点距 {row.spacing_mm:g} mm，深度 {row.depth_mm:g} mm")
    emit(f"排程：每排 {row.x_count} 个植入点，{len(plan.placements)} 次扎针动作（每次两针），"
          f"针距 {plan.needle_pitch_mm:g} mm，伺服目标 {plan.servo_target_mm:g} mm")
    emit(f"滑台行程 {row.slide_travel_mm:.4g} mm；"
          f"针长等差步长 {step:+.4g} mm（两针之间，这套针组只配 {row.angle_deg:+g}° 这个符号）")
    emit(f"区内覆盖 {row.covered_mm:g} mm，末端余量 {row.tail_mm:.4g} mm。")
    if row.count == 10:
        emit("20 mm 处不要求，第 10 根在 18 mm。")
    if row.count % 2:
        emit("补针时另一根针落在区外 30 mm；请确认余料可容纳，之后人工切除。")
    if datum is not None:
        head = plan.placements[0].cnc_x_mm
        tail = plan.placements[-1].cnc_x_mm
        # 最高点在排尾还是排头由 θ 的符号决定，而它决定排间抬刀会不会扎进管子，
        # 所以必须说出来，不能让操作员自己从两个数里推。
        side = "排尾" if row.slope >= 0 else "排头"
        emit(f"排内工作 Z：排头 {work_z_mm(plan, head, datum):+.3f} -> "
              f"排尾 {work_z_mm(plan, tail, datum):+.3f} mm"
              f"（最高 {highest_work_z_mm(plan, datum):+.3f} mm，在{side}）")


#: RunState 的枚举名是数据契约——reporting.py 把 .name 原样写进 JSON 摘要，
#: 所以枚举本身不能改。终端上要走这张表，两者互不影响。
_STATE_CN = {RunState.READY: "就绪", RunState.RUNNING: "运行中",
             RunState.WAITING_OPERATOR: "等待操作员", RunState.COMPLETE: "已完成",
             RunState.STOPPED: "已停止", RunState.FAULT: "故障"}


def _result_line(result) -> str:
    return (f"{_STATE_CN[result.state]}：{result.completed_placements} 个落点，"
            f"共 {result.completed_rows} 排（每排 {result.placements_per_row} 个）"
            + (f"；{result.error}" if result.error else ""))


def run_simulation(row: PresetSpec, *, inject_failure: bool = False, rows: int = 1,
                   datum=SMOKE_DATUM, scan_z_mm: float = SMOKE_SCAN_Z_MM, plan=None) -> int:
    if plan is None:
        plan = plan_preset_row(row, _known_geometry())
    from master.wizard import _build_motion
    motion = _build_motion(row)
    rig = SimulationRig(standby_mm=motion.standby_mm, scan_z_mm=scan_z_mm,
                        fail_on_first_descent=inject_failure,
                        slope=plan.slope, datum=datum)
    rig.servo.enable()
    rig.servo.set_zero()
    rig.head.zero()
    coordinator = Coordinator(rig.servo, rig.cnc, rig.head, motion,
                              scan_z_mm=scan_z_mm, operator_confirmed=True,
                              z_datum=datum, valve_delay_s=0,
                              confirm_next=lambda done, total, cancel: not cancel.is_set())
    _print_plan(row, plan, datum=datum)
    confirm_next_row = None
    if rows > 1:
        confirm_next_row = lambda completed_rows, cancel: (   # noqa: E731
            not cancel.is_set() and completed_rows < rows)
    result = coordinator.run(plan, confirm_next_row=confirm_next_row)
    print(_result_line(result))
    print("阀门已关闭" if rig.valves_closed else "阀门状态不安全")
    if rig.geometry_errors:
        for message in rig.geometry_errors:
            print(f"几何校验：{message}")
    return 0 if (result.state is RunState.COMPLETE and rig.valves_closed
                 and not rig.geometry_errors) else 1


def _read_live_configuration(path: Path, confirmed: bool,
                             job_overrides: dict[str, float] | None = None, *,
                             datum: tuple[float, float] | None = None, job_source=None,
                             row_y_mm: float | None = None, row_x_mm: float | None = None):
    if not confirmed:
        raise ValueError("配置：实机模式必须给出 --confirm-datum")
    # 模板 + 同级的 config.local.toml（向导生成）。本机文件不存在时完全等价于只读模板。
    cfg = load_merged(path)

    def required(section: str, key: str):
        value = cfg.get(section, {}).get(key)
        if value is None or value == "":
            raise ValueError(f"配置：[{section}] {key} 必填")
        return value

    source = job_source or PresetJobSource()
    row = source.build(cfg, job_overrides or {})
    geometry = _known_geometry()
    required("cnc3d", "port")
    required("head", "port")
    if cfg["cnc3d"]["port"] == cfg["head"]["port"]:
        raise ValueError("配置：CNC 与滑台不能使用同一个串口")
    if required("head", "firmware_verified") is not True:
        raise ValueError("配置：head firmware_verified 必须为 true")
    if datum is None:
        raise ValueError("配置：实机模式必须给出对刀点 (x0, z0)")
    if len(datum) != 2 or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                  and math.isfinite(value) for value in datum):
        raise ValueError("配置：对刀点 (x0, z0) 必须是两个有限数")
    # insertion_mm 不再手填：深度沿垂直管面量，滑台行程 = depth/cos(theta)。
    # [head] 段仍然要有待机与工作窗口，但下行目标是推导出来的。
    cut_clearance = validate_cut_clearance_mm(cfg.get("cycle", {}).get("cut_clearance_mm", 0.0))
    standby = required("head", "standby_mm")
    motion = HeadMotion(standby_mm=standby,
                        insertion_mm=standby-row.slide_travel_mm,
                        work_min_mm=required("head", "work_min_mm"),
                        work_max_mm=required("head", "work_max_mm"),
                        datum_confirmed=required("head", "datum_confirmed"),
                        cut_clearance_mm=cut_clearance)
    if not (HEAD_WORK_MIN_MM <= motion.work_min_mm < motion.work_max_mm <= HEAD_WORK_MAX_MM):
        raise ValueError(f"配置：head 工作窗口必须位于固件限位 [{HEAD_WORK_MIN_MM:g}, {HEAD_WORK_MAX_MM:g}] mm 内")
    recorded_safe = cfg.get("head", {}).get("cut_safe_mm", motion.cut_position_mm)
    if (isinstance(recorded_safe, bool) or not isinstance(recorded_safe, (int, float))
            or not math.isfinite(recorded_safe) or abs(recorded_safe-motion.cut_position_mm) > 0.05):
        raise ValueError("配置：记录的 GX28 剪丝安全位置与对刀待机位、上升距离不一致；请重新对刀")
    if not cut_clearance and required("cycle", "upper_release_after_retract") is not True:
        raise ValueError("配置：只支持已在实机验证过的上阀释放流程")
    scan_z = required("cnc3d", "scan_z_mm")
    if (isinstance(scan_z, bool) or not isinstance(scan_z, (int, float))
            or not math.isfinite(scan_z)):
        raise ValueError("配置：scan_z_mm 必须是有限数")
    plan = source.plan(row, geometry)
    if row_x_mm is not None:
        plan = plan_at_x(plan, row_x_mm)
    if row_y_mm is not None:
        plan_at_y(plan, row_y_mm)  # 仍验证对刀 Y 为有限数；实际绑定在运行时进行。
    # 排间安全高度必须高过排内**最高**的工作点。theta 带符号：正角时最高点在排尾、
    # 负角时在排头。写成 min 或假定单调，负角下"抬到安全高度"会变成向下扎进管子。
    highest = highest_work_z_mm(plan, datum)
    if scan_z + 1e-9 < highest:
        raise ValueError(
            f"配置：scan_z_mm {scan_z:g} mm 低于排内最高工作点 {highest:+.3f} mm；"
            f"照这个高度抬刀，排间移动会扎进管子")
    return cfg, row, geometry, motion, scan_z, plan, datum


def _read_stop_commands(commands: queue.Queue[str], *, echo=None,
                        stopped: threading.Event | None = None) -> None:
    """后台读急停指令；`stopped` 置起后读到什么都不再投递。

    这个线程阻塞在 input() 上，从外面打断不了——所以它是 daemon，运行结束后也不会
    自己退出。加这道闸是为了让它在运行**结束后**读到的那一行不被当成指令：否则操作员
    敲给下一个提示（菜单、下一次运行）的那一下会被这个线程吃掉。
    """
    prompt = "运行中，回车确认下一步，输入 ! 急停 > "
    while True:
        try:
            if echo is not None:
                # 回显行会 \r 擦掉提示行，所以擦之前先让它知道提示语是什么。
                echo.hold_prompt(prompt)
            line = input(prompt).strip()
        except EOFError:
            if stopped is None or not stopped.is_set():
                commands.put("EOF")
            return
        finally:
            if echo is not None:
                echo.release_prompt()
        if stopped is not None and stopped.is_set():
            return
        commands.put(line)


# 点动单步上限。机构余量很小，人眼也跟不上更大的步长。
JOG_LIMIT_MM = 0.5

SAFETY_MENU = (
    ("1", "使能伺服"),
    ("2", "点动伺服（小步，每次最多 0.5 mm）"),
    ("3", "伺服设零（把当前点记为本次连接的伺服零位）"),
    ("4", "滑台设零（确认待机位并发送 z）"),
    ("5", "开始植入"),
    ("0", "退出（不开始）"),
)

JOG_DIRECTIONS = (
    ("1", "靠紧（正，工位间距变小）"),
    ("2", "张开（负，工位间距变大）"),
    ("0", "返回"),
)


def _ask_jog(menu) -> float | None:
    """点动方向做成选项：负距离张开、正距离靠紧，符号本身反直觉，问方向比记符号可靠。"""
    choice = menu.choose("点动方向", JOG_DIRECTIONS)
    if choice == "0":
        return None
    distance = menu.ask_float("点动距离 mm", minimum=0.0, maximum=JOG_LIMIT_MM)
    return (1.0 if choice == "1" else -1.0) * distance


def _prepare_live_zeros(servo, head, *, menu=None, servo_required=True) -> bool:
    """Confirm required zeros; optional servo setup remains available without blocking injection."""
    if menu is None:
        from master.menu import make_menu
        menu = make_menu()
    from master.wizard import WizardCancelled

    menu.say("警告：断电重启后，滑台和伺服都会把当前点当作软件零位；它不代表机械零位。")
    menu.say("先确认滑台处于安全待机位置；伺服应调整到靠紧的 2 mm 工位间距位置。")
    menu.say("已知方向：负距离张开、正距离靠紧；点动时须观察实际机构。")
    if getattr(servo, "connected", True):
        try:
            menu.say(f"伺服相对本次连接初始读数：{servo.setup_offset_mm:+.3f} mm（不是机械基准）。")
        except Exception as exc:
            if servo_required:
                raise
            menu.warn(f"伺服状态读取失败：{exc}；请人工核对当前机械针距。")
    elif not servo_required:
        menu.warn("伺服不可用，未自动调整针距；请人工核对当前机械针距。")
    while True:
        menu.say("--- 安全设置 ---")
        try:
            choice = menu.choose("操作", SAFETY_MENU)
            if choice == "1":
                servo.enable()
                menu.say("伺服已使能；请观察机构后再点动。")
            elif choice == "2":
                delta = _ask_jog(menu)
                if delta is None:
                    continue
                servo.jog_setup_mm(delta)
                menu.say(f"伺服已点动 {delta:+g} mm；相对连接初始读数 "
                         f"{servo.setup_offset_mm:+.3f} mm。请检查实际位置。")
            elif choice == "3":
                servo.set_zero()
                menu.say("伺服逻辑零位已记录为当前机械位置。")
            elif choice == "4":
                head.zero()
                menu.say("滑台当前待机位置已设为软件 0 mm。")
            elif choice == "5":
                # 只列**还缺**的项。固定文案会把已经做完的那项也报出来，
                # 操作员照着提示重做一遍是小事，怀疑自己做错了才是大事。
                missing = [label for label, done in
                           (("[3] 伺服设零", not servo_required or servo.origin_count is not None),
                            ("[4] 滑台设零", head.zero_confirmed))
                           if not done]
                if missing:
                    menu.warn("禁止启动：还缺 " + " 和 ".join(missing) + "。")
                    continue
                return True
            else:
                return False
        except WizardCancelled:
            return False
        except Exception as exc:
            menu.warn(f"设置失败：{exc}")


class RowTurnGate:
    """排间确认：一排跑完后等操作员剪丝并把管子转过一个周向位置。

    机器没有回转轴，所以程序既不校验也不提示转了多少度——只等人按确认。
    与 `OperatorGate` 同一套回车确认机制，只是文案不同；主循环按当前等待方分派。
    """

    def __init__(self, *, output=None):
        self.output = output if output is not None else sys.stdout
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._waiting = False
        self._answer = False

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._waiting

    def wait_for_turn(self, completed_rows: int, cancel: threading.Event) -> bool:
        with self._lock:
            self._ready.clear()
            self._waiting = True
            self._answer = False
        print(f"第 {completed_rows} 排已完成：滑台已抬到安全高度并回到排头。"
              "请剪断两根丝、把管子转过一个周向位置，确认后按回车继续下一排；"
              "不再跑下一排请输入 D；输入 ! 停止。",
              file=self.output, flush=True)
        try:
            while not cancel.is_set():
                if self._ready.wait(0.05):
                    return self._answer and not cancel.is_set()
            return False
        finally:
            with self._lock:
                self._waiting = False
                self._ready.clear()

    def submit(self, command: str) -> bool:
        with self._lock:
            if not self._waiting or command.strip():
                return False
            self._answer = True
            self._ready.set()
            return True

    def decline(self, command: str) -> bool:
        """DONE 是正常收工，不是急停——否则操作员会学会习惯性按 !，那才是危险。"""
        with self._lock:
            if not self._waiting or command.strip().upper() not in ("D", "DONE", "完成"):
                return False
            self._answer = False
            self._ready.set()
            return True


@dataclass
class LiveDevices:
    servo: object
    cnc: object
    head: object


class UnavailableDevice:
    """A disconnected component whose failure does not affect other components."""

    connected = False
    origin_count = None
    zero_confirmed = False
    setup_offset_mm = 0.0

    def __init__(self, label, reason):
        self.label, self.reason = label, str(reason)
        self.cancel_event = None

    def close(self):
        pass

    def disable(self):
        pass

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise RuntimeError(f"{self.label} 不可用：{self.reason}")
        return unavailable


def _make_echo(debug: bool):
    """调试输出关闭时返回 None —— 设备层的约定是 None = 全静默。

    只有给了 --debug（或启动菜单里打开）才会有回显对象，其余路径一个字节都不多打。
    """
    if not debug:
        return None
    from master.echo import DeviceEcho
    return DeviceEcho()


def _open_head_serial(port, baud, **kwargs):
    """打开滑台串口，但不碰 DTR/RTS——否则等于替操作员按了一次复位键。

    CH340 的 DTR/RTS 经板上三极管接到 ESP32 的 EN 和 GPIO0，所以
    serial.Serial(端口, 波特率, ...) 这种"构造即打开"的写法会在 open() 的瞬间热复位芯片。
    而 GPIO12 既是 MTDI strapping 脚、又接着 CNC Shield 上 A4988 的 EN：热复位时它被拉
    高，VDD_SDIO 掉到 1.8V，3.3V 的板载 flash 整个读不到 → ROM 报
    invalid header: 0xffffffff → RTCWDT 循环，且不会自行恢复。冷启动没这个竞态（5V 轨
    还在爬升），所以只有"连接"这一次会打坏它。见 findings.md 2026-09-28 追加。

    pyserial 的构造器不接受 dtr=/rts= 关键字，只能先构造、设属性、再 open()。
    延迟 import serial：仿真与自检路径不该因为没装 pyserial 就跑不起来。
    """
    import serial

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    for name, value in kwargs.items():
        setattr(ser, name, value)
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def _open_live_component(name, cfg, geometry, motion=None, *, echo=None):
    """Construct only the requested component; preserve the head's no-reset serial opener."""
    from master.devices import Cnc3D, InsertHead, ServoAxis
    section = {"servo": "servo", "cnc": "cnc3d", "head": "head"}[name]
    values = cfg.get(section, {})
    interface = "adapter" if name == "servo" else "port"
    if not values.get(interface):
        raise ValueError(f"[{section}] 未选择{interface}")
    if name == "servo":
        lead = values.get("lead_mm", geometry.servo_lead_mm)
        if not isinstance(lead, (int, float)) or isinstance(lead, bool) or not math.isfinite(lead) or abs(lead - geometry.servo_lead_mm) > 1e-4:
            raise ValueError("servo lead_mm 与实测标定冲突")
        return ServoAxis(adapter=values["adapter"], lead_mm=geometry.servo_lead_mm,
                         counts_per_rev=values["counts_per_rev"], vel_mm_s=values["vel_mm_s"], echo=echo)
    if name == "cnc":
        return Cnc3D(port=values["port"], baud=values["baud"], feed=values["feed"], echo=echo)
    return InsertHead(port=values["port"], baud=values.get("baud", 115200), travel_mm=values.get("travel_mm", 50.0),
                      motion=motion, echo=echo, serial_factory=_open_head_serial)


def _open_live_devices(cfg: dict, geometry: MachineGeometry, motion: HeadMotion, *,
                       echo=None) -> LiveDevices:
    """Live injection requires CNC/head; a servo error is reported independently."""
    try:
        servo = _open_live_component("servo", cfg, geometry, motion, echo=echo)
    except Exception as exc:
        print(f"警告：伺服不可用：{exc}；未自动调整针距，请人工核对。")
        servo = UnavailableDevice("伺服", exc)
    return LiveDevices(servo=servo,
                       cnc=_open_live_component("cnc", cfg, geometry, motion, echo=echo),
                       head=_open_live_component("head", cfg, geometry, motion, echo=echo))


def _connect_live_devices(devices: LiveDevices) -> None:
    print("连接设备前请先确认滑台位于安全待机点，伺服机构有点动余量；"
          "上电显示 0 不代表机械零位。")
    for label, device in (("GX28 滑台", devices.head),
                          ("CNC 三维模组", devices.cnc),
                          ("SV630N 伺服", devices.servo)):
        try:
            device.connect()
        except Exception as exc:
            if device is devices.servo:
                print(f"警告：{label} 连接失败：{exc}；未自动调整针距，请人工核对。")
                try:
                    device.close()
                except Exception as close_exc:
                    print(f"警告：关闭伺服失败：{close_exc}")
                devices.servo = UnavailableDevice(label, exc)
                continue
            # 串口/网卡打不开是操作员最可能遇到的失败；不该让他看到 Python 堆栈。
            raise RuntimeError(f"{label} 连接失败：{exc}") from exc


def _close_live_devices(devices: LiveDevices) -> list[str]:
    """收尾：关阀、卸伺服使能、关三路。返回没能确认完成的动作，并逐条上屏。

    这里原本是三处 `except Exception: pass`。收尾失败被静默吞掉，等于「程序说它关好了」
    和「它真的关好了」之间的差别没人看得见——而关阀正是安全动作本身。

    收尾仍要全部做完：一个失败不该拦住后面两路关闭。所以是先收集、最后统一报。
    """
    failures: list[str] = []
    if devices.head.connected:
        for which in ("a", "b"):
            try:
                devices.head.valve(which, False)
            except Exception as exc:
                failures.append(f"关闭 {which} 阀失败：{exc}")
    if getattr(devices.servo, "connected", True):
        try:
            devices.servo.disable()
        except Exception as exc:
            failures.append(f"伺服卸使能失败：{exc}")
    for name, device in (("滑台", devices.head), ("三维模组", devices.cnc),
                         ("伺服", devices.servo)):
        try:
            device.close()
        except Exception as exc:
            failures.append(f"关闭{name}失败：{exc}")
    for message in failures:
        print(f"警告：{message}")
    return failures


def _make_reporter(row, plan, motion, scan_z, results_dir, *, datum=None):
    if results_dir is None:
        return None
    from master.reporting import RunReporter
    try:
        return RunReporter(job=row, plan=plan, machine=_known_geometry(), motion=motion,
                           scan_z_mm=scan_z, datum=datum, out_dir=results_dir)
    except Exception as exc:
        print(f"警告：无法创建结果文件（{exc}），本次运行不记录。")
        return None


def _execute_live_job(cfg: dict, row: PresetSpec, motion: HeadMotion, scan_z: float,
                      plan, devices: LiveDevices, *, datum=None, rows: int | None = None,
                      results_dir: Path | None = None, echo=None,
                      command_reader=None, row_y_mm: float | None = None) -> int:
    if row_y_mm is None:
        position = devices.cnc.position()
        if position is None:
            raise RuntimeError("无法读取 CNC 当前 Y，未开始自动植入")
        row_y_mm = position[1]
    plan = plan_at_y(plan, row_y_mm)
    print(f"本排固定 CNC Y={row_y_mm:g} mm（本次对刀位置）；植入及换排回排头均保持此 Y。")
    gate = OperatorGate(cut_clearance_mm=motion.cut_clearance_mm)
    turn_gate = RowTurnGate()
    reporter = _make_reporter(row, plan, motion, scan_z, results_dir, datum=datum)
    coordinator = Coordinator(devices.servo, devices.cnc, devices.head, motion,
                              scan_z_mm=scan_z, operator_confirmed=True, z_datum=datum,
                              valve_delay_s=cfg["cycle"]["valve_delay_ms"] / 1000,
                              confirm_next=gate.wait_for_ready,
                              servo_required=False, warning=lambda message: print(f"警告：{message}"),
                              progress=reporter)

    def confirm_next_row(completed_rows: int, cancel: threading.Event) -> bool:
        """一排跑完 → 抬到安全高度、回排头 → 等人工剪丝转管。默认一直跑下去。"""
        if rows is not None and completed_rows >= rows:
            return False
        return turn_gate.wait_for_turn(completed_rows, cancel)

    print((f"实时运行：每次退针后 GX28 额外上抬 {motion.cut_clearance_mm:g} mm，B 阀保持夹紧；"
           "剪丝完成并让双手离开后按回车，松开 B 阀并保持剪丝安全位。最后一次同样需要确认；"
           if motion.cut_clearance_mm else
           "实时运行：每次两针退回且 B 阀排气后，请剪丝并按回车移动到下一落点；")
          + "一排跑完后 CNC 抬到安全高度并回到排头，转管后按回车继续下一排；"
          "输入 D 正常收工，输入 ! 请求三路停止。")
    commands: queue.Queue[str] = queue.Queue()
    stop_reading = threading.Event()
    # 指令来源可替换：终端里是 `_read_stop_commands` 阻塞在 input() 上，图形界面里
    # 没有终端可读，改由按钮往同一个队列投递。**下面那条分派循环因此一行都不用改**
    # ——急停仍然是 "!"、继续仍然是空串、收工仍然是 "D"，走的还是同一条安全路径。
    reader_fn = _read_stop_commands if command_reader is None else command_reader
    reader = threading.Thread(target=reader_fn, args=(commands,),
                              kwargs={"echo": echo, "stopped": stop_reading}, daemon=True)
    reader.start()
    holder: list = []

    def execute() -> None:
        try:
            holder.append(coordinator.run(plan, confirm_next_row=confirm_next_row))
        except Exception as exc:
            holder.append(exc)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            command = commands.get(timeout=0.1)
            if command in ("!", "EOF"):
                coordinator.request_stop()
            elif turn_gate.decline(command):
                print("不再跑下一排，本次作业正常收工。")
            elif command.strip() == "":
                if not (gate.submit(command) or turn_gate.submit(command)):
                    print("当前没有等待中的人工确认。")
            else:
                print("当前需要按回车继续；输入 D 可在排间正常收工，! 可停止。")
        except queue.Empty:
            pass
        except KeyboardInterrupt:
            coordinator.request_stop()
    worker.join()
    # 运行结束：让读取线程读到下一行时闭嘴。它多半还阻塞在 input() 上，join 不回来，
    # 所以只置标志不 join——置了标志它就绝不会再把后续输入当成急停指令。
    stop_reading.set()
    if not holder or isinstance(holder[0], Exception):
        if reporter is not None:
            reporter.close()
        raise RuntimeError(f"实机总控在动作前失败：{holder[0] if holder else '没有返回结果'}")
    result = holder[0]
    print(_result_line(result))
    for message in coordinator.progress_errors:
        print(f"警告：结果记录失败：{message}")
    if reporter is not None:
        artifacts = reporter.finish(result)
        # reporter.errors 是记录器自己攒下的失败（开不了文件、写不进行、摘要没写成）。
        # 它不会抛异常——_fail() 只往列表里追加——所以 progress_errors 那条通道永远
        # 捕不到它，除了这里没有第二个地方会告诉操作员"这份记录不完整"。JSON 里的
        # recording_errors 是给机器看的；人得看屏幕。
        for message in reporter.errors:
            print(f"警告：结果记录失败：{message}")
        if artifacts is not None:
            print(f"结果已保存：{artifacts[0]}；摘要 {artifacts[1]}")
        else:
            print("警告：本次结果文件没有写成，跑过的落点无从追溯。")
    return 0 if result.state is RunState.COMPLETE else 1


def run_live(config_path: Path, confirmed: bool,
             job_overrides: dict[str, float] | None = None, *,
             datum: tuple[float, float] | None = None, rows: int | None = None,
             results_dir: Path | None = None, debug: bool = False,
             menu=None, command_reader=None, job_source=None) -> int:
    """实机总入口。

    `menu` 和 `command_reader` 是给图形界面的两个注入点，留空就是原来的终端行为：
    安全设置菜单需要一个能说话的对象，急停指令需要一个能读的来源——打包成无控制台
    窗口时 `sys.stdin` 是 None，这两处原样走 `input()` 会直接抛异常。
    """
    cfg, row, geometry, motion, scan_z, plan, datum = _read_live_configuration(
        config_path, confirmed, job_overrides, datum=datum, job_source=job_source)
    echo = _make_echo(debug)
    devices = _open_live_devices(cfg, geometry, motion, echo=echo)
    if results_dir is None:
        results_dir = Path(config_path).parent / "runs"
    try:
        _connect_live_devices(devices)
        _print_plan(row, plan, datum=datum)
        print(f"连接后状态：CNC WPos={devices.cnc.position()}，"
              f"滑台软件读数={devices.head.position_mm():.3f} mm；"
              f"预期安全高度 Z={scan_z:g} mm，对刀点 X={datum[0]:g} Z={datum[1]:g} mm。"
              f"读数不能证明机械零位。")
        if not _prepare_live_zeros(devices.servo, devices.head, menu=menu, servo_required=False):
            print("已取消，未开始自动植入。")
            return 1
        return _execute_live_job(cfg, row, motion, scan_z, plan, devices, datum=datum,
                                 rows=rows, results_dir=results_dir, echo=echo,
                                 command_reader=command_reader)
    finally:
        _close_live_devices(devices)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-pin five-head controller")
    # 三个模式参数保留原样、且仍然互斥；不带任何模式参数时才进菜单。
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-test", action="store_true", help="run full reference job without hardware")
    mode.add_argument("--simulate", action="store_true", help="run a specified job without hardware")
    mode.add_argument("--live", action="store_true", help="connect to actual machine after preflight")
    parser.add_argument("--angle", type=float, choices=PRESET_ANGLES,
                        help="架子角度 θ（0、15、30 度；管面朝 +x 上升）")
    parser.add_argument("--count", type=int, help="每排植入根数（菜单预设 7/10/15）")
    parser.add_argument("--depth", type=float, help="沿垂直管面的植入深度 mm（1-3）")
    parser.add_argument("--datum-x", type=float, help="对刀点的 CNC X mm（非交互实机路径必填）")
    parser.add_argument("--datum-z", type=float, help="对刀点的 CNC Z mm（非交互实机路径必填）")
    parser.add_argument("--rows", type=int,
                        help="实机跑多少排后正常收工；不给就一直跑到操作员叫停")
    parser.add_argument("--inject-failure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--confirm-datum", action="store_true",
                        help="confirm manual CNC work zero, safe scan height and head datum")
    parser.add_argument("--results-dir", type=Path,
                        help="directory for the per-placement CSV and JSON summary")
    parser.add_argument("--debug", action="store_true",
                        help="回显设备每一行回话；CNC 的 ? 状态轮询折叠为状态变化")
    args = parser.parse_args(argv)
    # sys.stdin 可能是 None（pythonw、打包成无控制台窗口时），那时 isatty() 会直接抛异常。
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if not (args.smoke_test or args.simulate or args.live) and not interactive:
        # 非交互环境（管道、重定向、自动化）不给菜单，否则会挂在那里等输入。
        parser.error("需要 --smoke-test / --simulate / --live 之一；"
                     "在终端里不带参数运行才会进入菜单")
    try:
        if not (args.smoke_test or args.simulate or args.live):
            from master.menu import run_startup_menu
            return run_startup_menu(config_path=args.config,
                                    results_dir=args.results_dir, debug=args.debug)
        if args.live:
            overrides = {key: value for key, value in
                         (("angle_deg", args.angle), ("count", args.count), ("depth_mm", args.depth))
                         if value is not None}
            if not args.confirm_datum:
                # 没有 --confirm-datum 就进交互向导；分派放在这里而不是 run_live 里，
                # 否则 run_live(confirmed=False) 的"连接前拒绝"语义会被向导吞掉。
                from master.wizard import run_setup_wizard
                return run_setup_wizard(args.config, job_overrides=overrides,
                                        results_dir=args.results_dir, debug=args.debug)
            if (args.datum_x is None) != (args.datum_z is None):
                raise ValueError("配置：--datum-x 和 --datum-z 必须一起给出")
            datum = None if args.datum_x is None else (args.datum_x, args.datum_z)
            extra = {} if args.results_dir is None else {"results_dir": args.results_dir}
            if args.rows is not None:
                # --rows 是"跑完第 N 排就正常收工"，而判据在**每一排跑完之后**才看
                # （coordinator 先跑排、再调 confirm_next_row）。所以 0 或负数不是
                # "不跑"，是"跑完第一排才发现该停"——机器会真的走完一整排。宁可在这里
                # 拒掉，也不能让操作员以为它什么都不做。
                if args.rows < 1:
                    raise ValueError(f"配置：--rows 必须 ≥ 1（给的是 {args.rows}）")
                extra["rows"] = args.rows
            return run_live(args.config, args.confirm_datum, job_overrides=overrides,
                            datum=datum, debug=args.debug, **extra)
        row = PresetSpec(*[value if value is not None else SMOKE_JOB[key]
                        for key, value in (("angle_deg", args.angle),
                                           ("count", args.count),
                                           ("depth_mm", args.depth))])
        return run_simulation(row, inject_failure=args.inject_failure,
                              rows=SMOKE_ROWS if args.smoke_test else 1)
    # OSError 必须显式列出：TimeoutError、SerialException、FileNotFoundError 都是它
    # 的子类，一个都不在上面四种里。漏掉它们的后果不是少一句提示——异常会穿透出去，
    # 打包后的 exe 把 traceback 打到 stderr 就退出，而双击启动没有 pause，窗口直接消失。
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
