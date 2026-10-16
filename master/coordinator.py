"""Shared job state machine for hardware and simulated device adapters."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from master.model import HeadMotion
from master.operator_gate import REPEAT
from master.planner import RowPlan, work_z_mm


class RunState(Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING_OPERATOR = "waiting_operator"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAULT = "fault"


@dataclass(frozen=True)
class RunResult:
    state: RunState
    completed_placements: int    # counted across every row that ran
    placements_per_row: int
    completed_rows: int          # rows that ran to the end of their placement list
    error: str | None = None


class _StopRequested(Exception):
    pass


def threading_from_config(cycle: dict) -> bool:
    enabled = cycle.get("wire_threading_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("配置：[cycle] wire_threading_enabled 必须是 true 或 false")
    return enabled


def validate_threading_motion(motion: HeadMotion) -> float:
    target = motion.standby_mm + 2.0  # 2 mm along the slide, not normal to the tube.
    if not math.isfinite(target) or not motion.work_min_mm <= target <= motion.work_max_mm:
        raise ValueError("套丝位置（对刀位上方 2 mm）超出已确认的滑台工作窗口")
    return target


class Coordinator:
    def __init__(self, servo, cnc, head, motion: HeadMotion, *, scan_z_mm: float,
                 operator_confirmed: bool, z_datum: tuple[float, float] | None = None,
                 valve_delay_s: float = 0.5,
                 confirm_next: Callable[[int, int, threading.Event], bool] | None = None,
                 progress=None, servo_required: bool = True, warning=None,
                 wire_threading_enabled: bool = False, first_injection: bool = True):
        if not math.isfinite(scan_z_mm) or not math.isfinite(valve_delay_s) or valve_delay_s < 0:
            raise ValueError("scan_z_mm 和 valve_delay_s 必须是有限数，且延时不能为负")
        if z_datum is not None:
            for name, value in zip(("x0", "z0"), z_datum):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"对刀点 {name} 必须是有限数")
        self.servo, self.cnc, self.head = servo, cnc, head
        self.motion = motion
        if not isinstance(wire_threading_enabled, bool) or not isinstance(first_injection, bool):
            raise ValueError("套丝开关与首次植入标记必须是 bool")
        self.wire_threading_enabled = wire_threading_enabled
        self._threading_position = (validate_threading_motion(motion)
                                    if wire_threading_enabled else None)
        # Attempts count independently of accepted placements, including repeats/rows.
        self._has_injected = not first_injection
        self.scan_z_mm = scan_z_mm
        #: (x0, z0) read at touch-off. The work height is z0 + (x - x0)*tan(theta):
        #: within a row the needles stay parallel to the tube, so the head has to
        #: follow x or the leading needles dig into the wall.
        self.z_datum = z_datum
        self.operator_confirmed = operator_confirmed
        self.valve_delay_s = valve_delay_s
        self.confirm_next = confirm_next
        self.progress = progress
        self.servo_required = servo_required
        self.warning = warning or print
        self.progress_errors: list[str] = []
        self.state = RunState.READY
        self.cancel_event = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_errors: list[str] = []
        for device in (self.servo, self.cnc, self.head):
            device.cancel_event = self.cancel_event

    def _notify(self, method: str, *args, **kwargs) -> None:
        """进度回调绝不能打断机器：记录失败，运行继续。

        但也不能静默吞掉——失败的次数和原因留在 progress_errors 里给 CLI 打印，
        否则一个坏掉的记录器会让整份结果文件凭空消失。
        """
        if self.progress is None:
            return
        try:
            getattr(self.progress, method)(*args, **kwargs)
        except Exception as exc:
            self.progress_errors.append(f"{method}: {exc}")

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise _StopRequested()

    def _delay(self) -> None:
        if self.cancel_event.wait(self.valve_delay_s):
            raise _StopRequested()

    def _check_ready(self, plan: RowPlan) -> None:
        if not self.operator_confirmed:
            raise RuntimeError("作业前必须由操作员确认 CNC 工作零点并保证行程无障碍")
        for name, device in (("SV630N 伺服", self.servo), ("CNC 三维模组", self.cnc),
                             ("GX28 滑台", self.head)):
            if not device.connected and (device is not self.servo or self.servo_required):
                raise RuntimeError(f"{name} 未连接")
        if self.servo_required and self.servo.origin_count is None:
            raise RuntimeError("伺服逻辑零位必须在机械靠紧位设置")
        if not self.head.zero_confirmed:
            raise RuntimeError("滑台软件零位必须在物理待机位确认")
        if self.z_datum is None:
            raise RuntimeError("作业前必须通过对刀确认对刀点")
        position = self.cnc.position()
        if position is None or abs(position[2] - self.scan_z_mm) > 0.05:
            raise RuntimeError("作业前 CNC 必须位于已确认的 scan_z_mm 高度")
        if not math.isfinite(position[1]) or abs(position[1] - plan.placements[0].cnc_y_mm) > 0.05:
            raise RuntimeError("作业前 CNC Y 与对刀时确认的位置不一致；请重新对刀，抬刀时只调整 Z")
        self._check_head_position()

    def _check_single_ready(self) -> None:
        """Validate the components needed by the manual one-shot action.

        A one-shot injection deliberately leaves the CNC where the operator put
        it.  Reusing ``_check_ready`` here would make the diagnostic action
        depend on a CNC Z datum and would invite an unexpected CNC move, so the
        The CNC object is still carried by the coordinator for the common
        emergency-stop path, but it is not queried or moved here.
        """
        for name, device in (("SV630N 伺服", self.servo),
                             ("GX28 滑台", self.head)):
            if not device.connected and (device is not self.servo or self.servo_required):
                raise RuntimeError(f"{name} 未连接")
        if self.servo_required and self.servo.origin_count is None:
            raise RuntimeError("伺服逻辑零位必须先设置")
        if not self.head.zero_confirmed:
            raise RuntimeError("滑台软件零位必须先设置")
        self._check_head_position()

    def _check_head_position(self) -> None:
        verify = getattr(self.head, "verify_work_window", None)
        if verify is not None:
            verify(self.motion)
        position = self.head.position_mm()
        expected = (self.motion.standby_mm, self.motion.cut_position_mm)
        if not math.isfinite(position) or all(abs(position - target) > 0.05 for target in expected):
            raise RuntimeError("滑台不在已确认的对刀待机位或剪丝安全位置；请重新核实位置")

    def _prepare_standby(self) -> None:
        self._check_cancel()
        self._check_head_position()
        if abs(self.head.position_mm() - self.motion.standby_mm) > 0.05:
            # Lower only after the CNC has reached the next insertion position.
            self.head.valve("a", False)
            self.head.valve("b", False)
            self._delay()
            self.head.move_mm(self.motion.standby_mm)
            self._check_cancel()

    def _prepare_travel(self) -> None:
        """Keep the head at the recorded cutting height throughout CNC travel."""
        self._check_cancel()
        self._check_head_position()
        if abs(self.head.position_mm() - self.motion.cut_position_mm) > 0.05:
            self.head.valve("a", False)
            self.head.valve("b", False)
            self._delay()
            self.head.move_mm(self.motion.cut_position_mm)
            self._check_cancel()
        position = self.head.position_mm()
        if (not self.head.zero_confirmed or not math.isfinite(position)
                or abs(position - self.motion.cut_position_mm) > 0.05):
            raise RuntimeError("滑台未到已记录的剪丝安全位；未移动 CNC")
        self._check_cancel()

    def _perform_cycle(self) -> None:
        if self.wire_threading_enabled and self._has_injected:
            self._check_cancel()
            self._check_head_position()
            self.head.valve("a", False)
            self._delay()
            self.head.valve("b", True)
            self._delay()
            self.head.move_mm(self._threading_position)
            self._check_cancel()
            position = self.head.position_mm()
            if (not self.head.zero_confirmed or not math.isfinite(position)
                    or abs(position-self._threading_position) > 0.05):
                raise RuntimeError("滑台未到表面上方 2 mm 套丝位置；未执行植入")
            self._check_cancel()
            self.head.valve("b", False)
            self._delay()
        else:
            self._prepare_standby()
        # Same absolute target: from the threading height this is old travel + 2 mm.
        self._cycle_one()
        self._has_injected = True

    def _position_servo(self, target_mm) -> None:
        if self.servo_required:
            self.servo.move_mm(target_mm)
            return
        try:
            if not self.servo.connected:
                raise RuntimeError("未连接")
            if self.servo.origin_count is None:
                raise RuntimeError("尚未设零")
            self.servo.move_mm(target_mm)
        except Exception as exc:
            self.warning(f"伺服未完成针距调整：{exc}；按当前机械针距继续，请人工核对。")

    def request_stop(self) -> None:
        """Callable from another thread while a device method is waiting."""
        with self._stop_lock:
            if self.cancel_event.is_set():
                return
            self.cancel_event.set()
            for name, device, action in (("滑台急停", self.head, self.head.stop),
                                         ("CNC 暂停", self.cnc, self.cnc.hold),
                                         ("伺服释放", self.servo, self.servo.disable)):
                if not device.connected:
                    continue
                try:
                    action()
                except Exception as exc:
                    self._stop_errors.append(f"{name}: {exc}")

    def _close_valves(self) -> list[str]:
        errors = []
        for which in ("a", "b"):
            try:
                self.head.valve(which, False)
            except Exception as exc:
                errors.append(f"关闭 {which} 阀：{exc}")
        return errors

    def _stop_reason(self, reason: str) -> str:
        errors = self._stop_errors + self._close_valves()
        if errors:
            return f"{reason}；安全状态未确认：{'; '.join(errors)}"
        return reason

    def _cycle_one(self) -> None:
        self._check_cancel()
        self.head.valve("a", True)
        self._delay()
        self.head.move_mm(self.motion.insertion_mm)
        self._check_cancel()
        self.head.valve("a", False)
        self._delay()
        self.head.valve("b", True)
        self._delay()
        self.head.move_mm(self.motion.standby_mm)
        self._check_cancel()
        if self.motion.cut_clearance_mm:
            self.head.move_mm(self.motion.cut_position_mm)
            self._check_cancel()
        else:
            self.head.valve("b", False)

    def _finish_cut(self) -> None:
        """Release the upper clamp after confirmation and hold the cutting height."""
        self._check_cancel()
        position = self.head.position_mm()
        if (not self.head.zero_confirmed or not math.isfinite(position)
                or abs(position-self.motion.cut_position_mm) > 0.05):
            raise RuntimeError("剪丝等待位置或滑台零位已改变；未进入下一动作，请重新核实位置")
        self.head.valve("b", False)
        self._delay()
        self._check_cancel()

    def _run_one_row(self, plan: RowPlan, row: int, total: int) -> None:
        """Walk the placement list once, incrementing ``self._completed``.

        The counter lives on the instance, not in a local: when a placement fails
        or the operator stops mid-row the count of everything that *did* finish
        still has to reach the result file. That is the only record of how far the
        job got.
        """
        for index, placement in enumerate(plan.placements):
            self._check_cancel()
            self._notify("placement_started", row, index, total, placement)
            try:
                self._prepare_travel()
                self.cnc.move_to(placement.cnc_x_mm, placement.cnc_y_mm,
                                 work_z_mm(plan, placement.cnc_x_mm, self.z_datum))
                self._check_cancel()
                self._perform_cycle()
            except BaseException as exc:
                # 落点中途失败或急停：必须留下一行，否则"跑到哪了"无从查起。
                self._notify("placement_finished", row, index, total, placement,
                             ok=False, error=str(exc) or type(exc).__name__)
                raise
            self._completed += 1
            self._notify("placement_finished", row, index, total, placement, ok=True, error=None)
            while self.motion.cut_clearance_mm or index + 1 < total:
                self.state = RunState.WAITING_OPERATOR
                self._check_cancel()
                decision = self.confirm_next(index + 1, total, self.cancel_event)
                if not decision:
                    raise _StopRequested()
                self._check_cancel()
                if decision == REPEAT:
                    self._completed -= 1  # the operator rejected the previous attempt
                    self._notify("placement_started", row, index, total, placement)
                    try:
                        self._repeat_cycle((placement.cnc_x_mm, placement.cnc_y_mm,
                                            work_z_mm(plan, placement.cnc_x_mm, self.z_datum)))
                    except BaseException as exc:
                        self._notify("placement_finished", row, index, total, placement,
                                     ok=False, error=str(exc) or type(exc).__name__)
                        raise
                    self._completed += 1
                    self._notify("placement_finished", row, index, total, placement, ok=True, error=None)
                    continue
                if self.motion.cut_clearance_mm:
                    self._finish_cut()
                self.state = RunState.RUNNING
                break

    def _repeat_cycle(self, cnc_position):
        """Repeat only the head/clamp cycle; never reposition the CNC or servo."""
        self._check_cancel()
        if cnc_position is not None:
            actual = self.cnc.position()
            if (actual is None or len(actual) != 3
                    or not all(math.isfinite(v) and abs(v-target) <= 0.05
                               for v, target in zip(actual, cnc_position))):
                raise RuntimeError("CNC 已离开本次植入位置，未执行原位重做")
        self._finish_cut()
        self.state = RunState.RUNNING
        self._perform_cycle()

    def run(self, plan: RowPlan, *, confirm_next_row=None) -> RunResult:
        """Run the row; ``confirm_next_row`` repeats it after the operator turns the tube.

        ``confirm_next_row`` is None for a single-row job (the behavior every
        existing caller gets). When given, it is called after each completed row
        once the head has been parked back at the safety height over the row head;
        returning False means "done", which is a normal finish, not a stop. The
        tube rotation itself is manual -- there is no rotary axis, so the program
        only waits for the operator.
        """
        if self.state is not RunState.READY:
            raise RuntimeError("总控不能重复运行，也不能在停止后恢复")
        total = len(plan.placements)
        if (total > 1 or self.motion.cut_clearance_mm) and self.confirm_next is None:
            raise RuntimeError("剪丝并重新装丝后，每次进入下一落点都需要操作员确认")
        self._check_ready(plan)
        self._completed = 0
        completed_rows = 0
        head = plan.placements[0]
        self.state = RunState.RUNNING
        try:
            self._check_cancel()
            self._position_servo(plan.servo_target_mm)
            self._check_cancel()
            while True:
                self._run_one_row(plan, completed_rows, total)
                completed_rows += 1
                if confirm_next_row is None:
                    break
                # 排间回到安全高度再回排头：自转重复是"一模一样的一排"。
                self._prepare_travel()
                self.cnc.move_to(head.cnc_x_mm, head.cnc_y_mm, self.scan_z_mm)
                self._check_cancel()
                self.state = RunState.WAITING_OPERATOR
                confirmed = confirm_next_row(completed_rows, self.cancel_event)
                # 排间等待时按了急停，回调会返回 False —— 那跟"操作员说 DONE"
                # 是两码事：一个是停止，一个是正常收工。先分辨再决定。
                self._check_cancel()
                if not confirmed:
                    break
                self.state = RunState.RUNNING
            self.state = RunState.COMPLETE
            return RunResult(self.state, self._completed, total, completed_rows)
        except _StopRequested:
            self.request_stop()
            self.state = RunState.STOPPED
            return RunResult(self.state, self._completed, total, completed_rows,
                             self._stop_reason("操作员请求停止"))
        except Exception as exc:
            if self.cancel_event.is_set():
                self.state = RunState.STOPPED
                return RunResult(self.state, self._completed, total, completed_rows,
                                 self._stop_reason("操作员请求停止"))
            self.request_stop()
            self.state = RunState.FAULT
            return RunResult(self.state, self._completed, total, completed_rows,
                             self._stop_reason(str(exc)))

    def run_single(self, plan: RowPlan) -> RunResult:
        """Perform exactly one five-needle cycle at the CNC's current position.

        This is the manual/debug path.  It uses the selected plan only for the
        servo expansion target and the head stroke; it never calls ``cnc.move_to``
        and does not require a CNC work datum. The servo can be optional.
        """
        if self.state is not RunState.READY:
            raise RuntimeError("总控不能重复运行，也不能在停止后恢复")
        if not plan.placements:
            raise ValueError("单次注入需要至少一个排程落点")
        self._check_single_ready()
        self._single_cnc_connected = self.cnc.connected
        self._single_cnc_position = None
        if self._single_cnc_connected:
            try:
                position = self.cnc.position()
                if position is not None and len(position) == 3 and all(math.isfinite(v) for v in position):
                    self._single_cnc_position = tuple(position)
            except Exception:
                pass  # a standalone stroke still does not require CNC communication
        self._completed = 0
        self.state = RunState.RUNNING
        try:
            self._check_cancel()
            self._position_servo(plan.servo_target_mm)
            self._check_cancel()
            self._perform_cycle()
            self._completed = 1
            if self.motion.cut_clearance_mm:
                self.state = RunState.WAITING_OPERATOR
                return RunResult(self.state, 1, 1, 0)
            self.state = RunState.COMPLETE
            return RunResult(self.state, 1, 1, 1)
        except _StopRequested:
            self.request_stop()
            self.state = RunState.STOPPED
            return RunResult(self.state, self._completed, 1, 0,
                             self._stop_reason("操作员请求停止"))
        except Exception as exc:
            if self.cancel_event.is_set():
                self.state = RunState.STOPPED
                return RunResult(self.state, self._completed, 1, 0,
                                 self._stop_reason("操作员请求停止"))
            self.request_stop()
            self.state = RunState.FAULT
            return RunResult(self.state, self._completed, 1, 0,
                             self._stop_reason(str(exc)))

    def repeat_single(self) -> RunResult:
        if self.state is not RunState.WAITING_OPERATOR or not self.motion.cut_clearance_mm:
            raise RuntimeError("没有等待剪丝的单次植入，不能原位重做")
        self._completed = 0
        try:
            if self._single_cnc_connected and self._single_cnc_position is None:
                raise RuntimeError("无法确认原植入位置，未执行原位重做")
            self._repeat_cycle(self._single_cnc_position)
            self._completed = 1
            self.state = RunState.WAITING_OPERATOR
            return RunResult(self.state, 1, 1, 0)
        except _StopRequested:
            self.request_stop()
            self.state = RunState.STOPPED
            return RunResult(self.state, 0, 1, 0, self._stop_reason("操作员请求停止"))
        except Exception as exc:
            cancelled = self.cancel_event.is_set()
            self.request_stop()
            self.state = RunState.STOPPED if cancelled else RunState.FAULT
            return RunResult(self.state, 0, 1, 0, self._stop_reason(str(exc)))

    def finish_single_cut(self) -> RunResult:
        if self.state is not RunState.WAITING_OPERATOR or not self.motion.cut_clearance_mm:
            raise RuntimeError("没有等待确认的单次剪丝动作")
        try:
            self._finish_cut()
            self.state = RunState.COMPLETE
            return RunResult(self.state, 1, 1, 1)
        except _StopRequested:
            self.request_stop()
            self.state = RunState.STOPPED
            return RunResult(self.state, 1, 1, 0, self._stop_reason("操作员请求停止"))
        except Exception as exc:
            self.request_stop()
            self.state = RunState.FAULT
            return RunResult(self.state, 1, 1, 0, self._stop_reason(str(exc)))
