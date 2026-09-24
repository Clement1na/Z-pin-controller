"""Z-pin CLI. Simulation and live operation share one planner and coordinator."""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from master.coordinator import Coordinator, RunState
from master.model import HeadMotion, JobSpec, MachineGeometry
from master.operator_gate import OperatorGate
from master.planner import plan_grid
from master.simulated import SimulationRig


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.toml"
    return Path(__file__).resolve().parent / "config.toml"


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
    try:
        with path.open("rb") as stream:
            cfg = tomllib.load(stream)
    except OSError as exc:
        raise ValueError(f"configuration: cannot read {path}: {exc}") from exc

    def required(section: str, key: str):
        value = cfg.get(section, {}).get(key)
        if value is None or value == "":
            raise ValueError(f"configuration: [{section}] {key} is required")
        return value

    job_defaults = {key: required("job", key) for key in
                    ("width_mm", "height_mm", "spacing_mm", "plate_thickness_mm")}
    overrides = job_overrides or {}
    unknown = set(overrides) - set(job_defaults)
    if unknown:
        raise ValueError(f"configuration: unknown job override {', '.join(sorted(unknown))}")
    job = JobSpec(**(job_defaults | overrides))
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


def _read_stop_commands(commands: queue.Queue[str]) -> None:
    while True:
        try:
            commands.put(input("运行中，输入 ! 急停 > ").strip())
        except EOFError:
            commands.put("EOF")
            return


def _prepare_live_zeros(servo, head) -> bool:
    print("警告：断电重启后，滑台和伺服都会把当前点当作软件零位；它不代表机械零位。")
    print("先确认滑台处于安全待机位置；伺服应调整到靠紧的 2 mm 工位间距位置。")
    print("伺服设置命令：ENABLE 使能；JOG ±毫米 小步点动（每次最多 0.5 mm）；"
          "SERVO-ZERO 设当前点为伺服零位；HEAD-ZERO 确认滑台待机并发送 z；RUN 开始；QUIT 退出。")
    print("已知方向：负距离张开、正距离靠紧；点动时须观察实际机构。")
    print(f"伺服相对本次连接初始读数：{servo.setup_offset_mm:+.3f} mm（不是机械基准）。")
    while True:
        try:
            command = input("安全设置 > ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            return False
        try:
            if command == "ENABLE":
                servo.enable()
                print("伺服已使能；请观察机构后再点动。")
            elif command.startswith("JOG "):
                delta = float(command.split(maxsplit=1)[1])
                servo.jog_setup_mm(delta)
                print(f"伺服已点动 {delta:+g} mm；相对连接初始读数 "
                      f"{servo.setup_offset_mm:+.3f} mm。请检查实际位置。")
            elif command == "SERVO-ZERO":
                servo.set_zero()
                print("伺服逻辑零位已记录为当前机械位置。")
            elif command == "HEAD-ZERO":
                head.zero()
                print("滑台当前待机位置已设为软件 0 mm。")
            elif command == "RUN":
                if servo.origin_count is None or not head.zero_confirmed:
                    print("禁止启动：请先完成 SERVO-ZERO 和 HEAD-ZERO。")
                    continue
                return True
            elif command in ("QUIT", "Q", "!"):
                return False
            else:
                print("无效命令；可用 ENABLE、JOG ±毫米、SERVO-ZERO、HEAD-ZERO、RUN、QUIT。")
        except (ValueError, RuntimeError, TimeoutError) as exc:
            print(f"设置失败：{exc}")


def run_live(config_path: Path, confirmed: bool,
             job_overrides: dict[str, float] | None = None) -> int:
    cfg, job, geometry, motion, scan_z, plan = _read_live_configuration(
        config_path, confirmed, job_overrides)
    from master.devices import Cnc3D, InsertHead, ServoAxis

    sv, cn, hd = cfg["servo"], cfg["cnc3d"], cfg["head"]
    servo = ServoAxis(adapter=sv["adapter"], lead_mm=geometry.servo_lead_mm,
                      counts_per_rev=sv["counts_per_rev"], vel_mm_s=sv["vel_mm_s"])
    cnc = Cnc3D(port=cn["port"], baud=cn["baud"], feed=cn["feed"],
                limits=(cn["x_max"], cn["y_max"], cn["z_max"]))
    head = InsertHead(port=hd["port"], baud=hd["baud"],
                      travel_mm=hd["travel_mm"], motion=motion)
    gate = OperatorGate()
    coordinator = Coordinator(servo, cnc, head, motion, scan_z_mm=scan_z,
                              operator_confirmed=confirmed,
                              valve_delay_s=cfg["cycle"]["valve_delay_ms"] / 1000,
                              confirm_next=gate.wait_for_ready)
    try:
        print("连接设备前请先确认滑台位于安全待机点，伺服机构有点动余量；"
              "上电显示 0 不代表机械零位。")
        head.connect()
        cnc.connect()
        servo.connect()
        _print_plan(job, plan)
        print(f"连接后状态：CNC WPos={cnc.position()}，滑台软件读数={head.position_mm():.3f} mm；"
              f"预期扫描 Z={scan_z:g} mm。读数不能证明机械零位。")
        if not _prepare_live_zeros(servo, head):
            print("已取消，未开始自动植入。")
            return 1
        print("实时运行：输入 ! 可请求三路停止。每次五针退回且 B 阀排气后，"
              "请剪丝并按屏幕提示输入 READY 代码，才会移动到下一落点。")
        commands: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=_read_stop_commands, args=(commands,), daemon=True)
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
        return 0 if result.state is RunState.COMPLETE else 1
    finally:
        if head.connected:
            for which in ("a", "b"):
                try:
                    head.valve(which, False)
                except Exception:
                    pass
        try:
            servo.disable()
        except Exception:
            pass
        for device in (head, cnc, servo):
            try:
                device.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-pin five-head controller")
    mode = parser.add_mutually_exclusive_group(required=True)
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
    args = parser.parse_args(argv)
    try:
        if args.live:
            overrides = {key: value for key, value in
                         (("width_mm", args.width), ("height_mm", args.height),
                          ("spacing_mm", args.spacing), ("plate_thickness_mm", args.thickness))
                         if value is not None}
            return run_live(args.config, args.confirm_datum, job_overrides=overrides)
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
