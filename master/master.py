"""Z-pin CLI. Simulation and live operation share one planner and coordinator."""

from __future__ import annotations

import argparse
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from master.coordinator import Coordinator, RunState
from master.local_config import load_merged, local_path_for, write_local
from master.model import HeadMotion, JobSpec, MachineGeometry
from master.operator_gate import OperatorGate
from master.planner import plan_grid
from master.simulated import SimulationRig


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.toml"
    return Path(__file__).resolve().parent / "config.toml"


JOB_KEYS = ("width_mm", "height_mm", "spacing_mm", "plate_thickness_mm")


def _known_geometry() -> MachineGeometry:
    return MachineGeometry(station_count=5, station_width_mm=10,
                           max_expansion_mm=10, servo_lead_mm=13.3333333)


def _print_plan(job: JobSpec, plan) -> None:
    print(f"Job: width {job.width_mm:g} mm, height {job.height_mm:g} mm, "
          f"spacing {job.spacing_mm:g} mm, thickness {job.plate_thickness_mm:g} mm")
    print(f"Plan: {job.point_count} points, {len(plan.placements)} placements, "
          f"needle pitch {plan.needle_pitch_mm:g} mm, servo {plan.servo_target_mm:g} mm")


def run_simulation(job: JobSpec, *, inject_failure: bool = False) -> int:
    plan = plan_grid(job, _known_geometry())
    motion = HeadMotion(standby_mm=0, insertion_mm=-5,
                        work_min_mm=-10, work_max_mm=0, datum_confirmed=True)
    rig = SimulationRig(standby_mm=motion.standby_mm, scan_z_mm=0,
                        fail_on_first_descent=inject_failure)
    rig.servo.enable()
    rig.servo.set_zero()
    rig.head.zero()
    coordinator = Coordinator(rig.servo, rig.cnc, rig.head, motion,
                              scan_z_mm=0, operator_confirmed=True, valve_delay_s=0,
                              confirm_next=lambda done, total, cancel: not cancel.is_set())
    _print_plan(job, plan)
    result = coordinator.run(plan)
    print(f"{result.state.name}: {result.completed_placements}/{result.total_placements} placements"
          + (f"; {result.error}" if result.error else ""))
    print("valves closed" if rig.valves_closed else "VALVE STATE UNSAFE")
    return 0 if result.state is RunState.COMPLETE and rig.valves_closed else 1


def _read_live_configuration(path: Path, confirmed: bool,
                             job_overrides: dict[str, float] | None = None):
    if not confirmed:
        raise ValueError("configuration: --confirm-datum is required for live mode")
    # 模板 + 同级的 config.local.toml（向导生成）。本机文件不存在时完全等价于只读模板。
    cfg = load_merged(path)

    def required(section: str, key: str):
        value = cfg.get(section, {}).get(key)
        if value is None or value == "":
            raise ValueError(f"configuration: [{section}] {key} is required")
        return value

    overrides = job_overrides or {}
    unknown = set(overrides) - set(JOB_KEYS)
    if unknown:
        raise ValueError(f"configuration: unknown job override {', '.join(sorted(unknown))}")
    # 模板刻意注释掉了 [job]：明确传入的覆盖项可以补齐缺失的键，没传的仍然必须来自配置。
    job_defaults = {key: overrides[key] if key in overrides else required("job", key)
                    for key in JOB_KEYS}
    job = JobSpec(**job_defaults)
    geometry = _known_geometry()
    lead = required("servo", "lead_mm")
    if abs(lead - geometry.servo_lead_mm) > 1e-4:
        raise ValueError("configuration: servo lead_mm conflicts with measured calibration")
    required("servo", "adapter")
    required("cnc3d", "port")
    required("head", "port")
    if cfg["cnc3d"]["port"] == cfg["head"]["port"]:
        raise ValueError("configuration: CNC and head must use different ports")
    if required("head", "firmware_verified") is not True:
        raise ValueError("configuration: head firmware_verified must be true")
    motion = HeadMotion(standby_mm=required("head", "standby_mm"),
                        insertion_mm=required("head", "insertion_mm"),
                        work_min_mm=required("head", "work_min_mm"),
                        work_max_mm=required("head", "work_max_mm"),
                        datum_confirmed=required("head", "datum_confirmed"))
    if not -10 <= motion.work_min_mm < motion.insertion_mm < motion.standby_mm == 0 == motion.work_max_mm:
        raise ValueError("configuration: head must use tested negative descent window [-10, 0] and standby zero")
    if required("cycle", "upper_release_after_retract") is not True:
        raise ValueError("configuration: only verified upper-valve release protocol is supported")
    scan_z = required("cnc3d", "scan_z_mm")
    if not 0 <= scan_z <= required("cnc3d", "z_max"):
        raise ValueError("configuration: scan_z_mm outside CNC Z limit")
    plan = plan_grid(job, geometry)
    if (max(p.cnc_x_mm for p in plan.placements) > required("cnc3d", "x_max")
            or max(p.cnc_y_mm for p in plan.placements) > required("cnc3d", "y_max")):
        raise ValueError("configuration: planned CNC location exceeds X/Y soft limit")
    return cfg, job, geometry, motion, scan_z, plan


def _read_stop_commands(commands: queue.Queue[str], *, echo=None) -> None:
    prompt = "运行中，输入 ! 急停 > "
    while True:
        try:
            if echo is not None:
                # 回显行会 \r 擦掉提示行，所以擦之前先让它知道提示语是什么。
                echo.hold_prompt(prompt)
            commands.put(input(prompt).strip())
        except EOFError:
            commands.put("EOF")
            return
        finally:
            if echo is not None:
                echo.release_prompt()


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


def _prepare_live_zeros(servo, head, *, menu=None) -> bool:
    """安全设置菜单。返回 True 表示可以开始自动植入。

    动硬件之前必须由操作员逐项确认：先使能、再小步点动看机构、然后把当前点记为
    零点。零点不满足就拒绝启动并说明缺哪一项——这一步不是走过场，断电重启后
    伺服逻辑零点必然被清空，正常一次开机都要走这里。
    """
    if menu is None:
        from master.menu import make_menu
        menu = make_menu()
    from master.wizard import WizardCancelled

    menu.say("警告：断电重启后，滑台和伺服都会把当前点当作软件零位；它不代表机械零位。")
    menu.say("先确认滑台处于安全待机位置；伺服应调整到靠紧的 2 mm 工位间距位置。")
    menu.say("已知方向：负距离张开、正距离靠紧；点动时须观察实际机构。")
    menu.say(f"伺服相对本次连接初始读数：{servo.setup_offset_mm:+.3f} mm（不是机械基准）。")
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
                           (("[3] 伺服设零", servo.origin_count is not None),
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
        except (ValueError, RuntimeError, TimeoutError) as exc:
            menu.warn(f"设置失败：{exc}")


@dataclass
class LiveDevices:
    servo: object
    cnc: object
    head: object


def _make_echo(debug: bool):
    """调试输出关闭时返回 None —— 设备层的约定是 None = 全静默。

    只有给了 --debug（或启动菜单里打开）才会有回显对象，其余路径一个字节都不多打。
    """
    if not debug:
        return None
    from master.echo import DeviceEcho
    return DeviceEcho()


def _open_live_devices(cfg: dict, geometry: MachineGeometry, motion: HeadMotion, *,
                       echo=None) -> LiveDevices:
    """只构造对象，不碰硬件。连接失败前不会有任何动作。"""
    from master.devices import Cnc3D, InsertHead, ServoAxis

    sv, cn, hd = cfg["servo"], cfg["cnc3d"], cfg["head"]
    return LiveDevices(
        servo=ServoAxis(adapter=sv["adapter"], lead_mm=geometry.servo_lead_mm,
                        counts_per_rev=sv["counts_per_rev"], vel_mm_s=sv["vel_mm_s"],
                        echo=echo),
        cnc=Cnc3D(port=cn["port"], baud=cn["baud"], feed=cn["feed"],
                  limits=(cn["x_max"], cn["y_max"], cn["z_max"]), echo=echo),
        head=InsertHead(port=hd["port"], baud=hd["baud"],
                        travel_mm=hd["travel_mm"], motion=motion, echo=echo))


def _connect_live_devices(devices: LiveDevices) -> None:
    print("连接设备前请先确认滑台位于安全待机点，伺服机构有点动余量；"
          "上电显示 0 不代表机械零位。")
    for label, device in (("GX28 滑台", devices.head),
                          ("CNC 三维模组", devices.cnc),
                          ("SV630N 伺服", devices.servo)):
        try:
            device.connect()
        except Exception as exc:
            # 串口/网卡打不开是操作员最可能遇到的失败；不该让他看到 Python 堆栈。
            raise RuntimeError(f"{label} 连接失败：{exc}") from exc


def _close_live_devices(devices: LiveDevices) -> None:
    if devices.head.connected:
        for which in ("a", "b"):
            try:
                devices.head.valve(which, False)
            except Exception:
                pass
    try:
        devices.servo.disable()
    except Exception:
        pass
    for device in (devices.head, devices.cnc, devices.servo):
        try:
            device.close()
        except Exception:
            pass


def _make_reporter(job, plan, motion, scan_z, results_dir):
    if results_dir is None:
        return None
    from master.reporting import RunReporter
    try:
        return RunReporter(job=job, plan=plan, machine=_known_geometry(), motion=motion,
                           scan_z_mm=scan_z, out_dir=results_dir)
    except Exception as exc:
        print(f"警告：无法创建结果文件（{exc}），本次运行不记录。")
        return None


def _execute_live_job(cfg: dict, job: JobSpec, motion: HeadMotion, scan_z: float,
                      plan, devices: LiveDevices, *, results_dir: Path | None = None,
                      echo=None) -> int:
    gate = OperatorGate()
    reporter = _make_reporter(job, plan, motion, scan_z, results_dir)
    coordinator = Coordinator(devices.servo, devices.cnc, devices.head, motion,
                              scan_z_mm=scan_z, operator_confirmed=True,
                              valve_delay_s=cfg["cycle"]["valve_delay_ms"] / 1000,
                              confirm_next=gate.wait_for_ready,
                              progress=reporter)
    print("实时运行：输入 ! 可请求三路停止。每次五针退回且 B 阀排气后，"
          "请剪丝并按屏幕提示输入 READY 代码，才会移动到下一落点。")
    commands: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_read_stop_commands, args=(commands,),
                              kwargs={"echo": echo}, daemon=True)
    reader.start()
    holder: list = []

    def execute() -> None:
        try:
            holder.append(coordinator.run(plan))
        except Exception as exc:
            holder.append(exc)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            command = commands.get(timeout=0.1)
            if command in ("!", "EOF"):
                coordinator.request_stop()
            elif command.upper().startswith("READY") and not gate.submit(command):
                print("确认码无效或当前未等待剪丝；下一落点仍未放行。")
        except queue.Empty:
            pass
        except KeyboardInterrupt:
            coordinator.request_stop()
    worker.join()
    if not holder or isinstance(holder[0], Exception):
        raise RuntimeError(f"live coordinator failed before motion: {holder[0] if holder else 'no result'}")
    result = holder[0]
    print(f"{result.state.name}: {result.completed_placements}/{result.total_placements} placements"
          + (f"; {result.error}" if result.error else ""))
    for message in coordinator.progress_errors:
        print(f"警告：结果记录失败：{message}")
    if reporter is not None:
        artifacts = reporter.finish(result)
        if artifacts is not None:
            print(f"结果已保存：{artifacts[0]}；摘要 {artifacts[1]}")
    return 0 if result.state is RunState.COMPLETE else 1


def run_live(config_path: Path, confirmed: bool,
             job_overrides: dict[str, float] | None = None, *,
             results_dir: Path | None = None, debug: bool = False) -> int:
    cfg, job, geometry, motion, scan_z, plan = _read_live_configuration(
        config_path, confirmed, job_overrides)
    echo = _make_echo(debug)
    devices = _open_live_devices(cfg, geometry, motion, echo=echo)
    if results_dir is None:
        results_dir = Path(config_path).parent / "runs"
    try:
        _connect_live_devices(devices)
        _print_plan(job, plan)
        print(f"连接后状态：CNC WPos={devices.cnc.position()}，"
              f"滑台软件读数={devices.head.position_mm():.3f} mm；"
              f"预期扫描 Z={scan_z:g} mm。读数不能证明机械零位。")
        if not _prepare_live_zeros(devices.servo, devices.head):
            print("已取消，未开始自动植入。")
            return 1
        return _execute_live_job(cfg, job, motion, scan_z, plan, devices,
                                 results_dir=results_dir, echo=echo)
    finally:
        _close_live_devices(devices)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-pin five-head controller")
    # 三个模式参数保留原样、且仍然互斥；不带任何模式参数时才进菜单。
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-test", action="store_true", help="run full reference job without hardware")
    mode.add_argument("--simulate", action="store_true", help="run a specified job without hardware")
    mode.add_argument("--live", action="store_true", help="connect to actual machine after preflight")
    parser.add_argument("--width", type=float)
    parser.add_argument("--height", type=float)
    parser.add_argument("--spacing", type=float)
    parser.add_argument("--thickness", type=float)
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
                         (("width_mm", args.width), ("height_mm", args.height),
                          ("spacing_mm", args.spacing), ("plate_thickness_mm", args.thickness))
                         if value is not None}
            if not args.confirm_datum:
                # 没有 --confirm-datum 就进交互向导；分派放在这里而不是 run_live 里，
                # 否则 run_live(confirmed=False) 的"连接前拒绝"语义会被向导吞掉。
                from master.wizard import run_setup_wizard
                return run_setup_wizard(args.config, job_overrides=overrides,
                                        results_dir=args.results_dir, debug=args.debug)
            extra = {} if args.results_dir is None else {"results_dir": args.results_dir}
            return run_live(args.config, args.confirm_datum, job_overrides=overrides,
                            debug=args.debug, **extra)
        job = JobSpec(args.width if args.width is not None else 360,
                      args.height if args.height is not None else 360,
                      args.spacing if args.spacing is not None else 4,
                      args.thickness if args.thickness is not None else 5)
        return run_simulation(job, inject_failure=args.inject_failure)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
