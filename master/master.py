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
    print(f"Plan: {job.point_count} points, {len(plan.placements)} placements, "
          f"needle pitch {plan.needle_pitch_mm:g} mm, servo {plan.servo_target_mm:g} mm")


def run_simulation(job: JobSpec, *, inject_failure: bool = False) -> int:
    plan = plan_grid(job, _known_geometry())
    motion = HeadMotion(standby_mm=10, insertion_mm=5,
                        work_min_mm=3, work_max_mm=13, datum_confirmed=True)
    rig = SimulationRig(standby_mm=motion.standby_mm, scan_z_mm=0,
                        fail_on_first_descent=inject_failure)
    coordinator = Coordinator(rig.servo, rig.cnc, rig.head, motion,
                              scan_z_mm=0, operator_confirmed=True, valve_delay_s=0)
    _print_plan(job, plan)
    result = coordinator.run(plan)
    print(f"{result.state.name}: {result.completed_placements}/{result.total_placements} placements"
          + (f"; {result.error}" if result.error else ""))
    print("valves closed" if rig.valves_closed else "VALVE STATE UNSAFE")
    return 0 if result.state is RunState.COMPLETE and rig.valves_closed else 1


def _read_live_configuration(path: Path, confirmed: bool):
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

    job = JobSpec(width_mm=required("job", "width_mm"),
                  height_mm=required("job", "height_mm"),
                  spacing_mm=required("job", "spacing_mm"),
                  plate_thickness_mm=required("job", "plate_thickness_mm"))
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
    if not 0 <= motion.work_min_mm < motion.work_max_mm <= required("head", "travel_mm"):
        raise ValueError("configuration: head work window exceeds firmware coordinate range")
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
            return


def run_live(config_path: Path, confirmed: bool) -> int:
    cfg, job, geometry, motion, scan_z, plan = _read_live_configuration(config_path, confirmed)
    from master.devices import Cnc3D, InsertHead, ServoAxis

    sv, cn, hd = cfg["servo"], cfg["cnc3d"], cfg["head"]
    servo = ServoAxis(adapter=sv["adapter"], lead_mm=geometry.servo_lead_mm,
                      counts_per_rev=sv["counts_per_rev"], vel_mm_s=sv["vel_mm_s"])
    cnc = Cnc3D(port=cn["port"], baud=cn["baud"], feed=cn["feed"],
                limits=(cn["x_max"], cn["y_max"], cn["z_max"]))
    head = InsertHead(port=hd["port"], baud=hd["baud"],
                      travel_mm=hd["travel_mm"], motion=motion)
    coordinator = Coordinator(servo, cnc, head, motion, scan_z_mm=scan_z,
                              operator_confirmed=confirmed,
                              valve_delay_s=cfg["cycle"]["valve_delay_ms"] / 1000)
    try:
        head.connect()
        cnc.connect()
        servo.connect()
        _print_plan(job, plan)
        print(f"连接后状态：CNC WPos={cnc.position()}，滑台={head.position_mm():.3f} mm；"
              f"预期扫描 Z={scan_z:g} mm、滑台待机={motion.standby_mm:g} mm。")
        if input("确认机械间隙、工作零点和供丝状态后，输入 RUN 开始：").strip() != "RUN":
            print("已取消，未开始自动植入。")
            return 1
        print("实时运行：按 Enter 后输入 ! 可请求三路停止。")
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
                if command == "!":
                    coordinator.request_stop()
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
    parser.add_argument("--width", type=float, default=360)
    parser.add_argument("--height", type=float, default=360)
    parser.add_argument("--spacing", type=float, default=4)
    parser.add_argument("--thickness", type=float, default=5)
    parser.add_argument("--inject-failure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--confirm-datum", action="store_true",
                        help="confirm manual CNC work zero, safe scan height and head datum")
    args = parser.parse_args(argv)
    try:
        if args.live:
            return run_live(args.config, args.confirm_datum)
        job = JobSpec(args.width, args.height, args.spacing, args.thickness)
        return run_simulation(job, inject_failure=args.inject_failure)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
