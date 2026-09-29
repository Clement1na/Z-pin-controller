"""Shared job state machine for hardware and simulated device adapters."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from master.model import HeadMotion
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


class Coordinator:
    def __init__(self, servo, cnc, head, motion: HeadMotion, *, scan_z_mm: float,
                 operator_confirmed: bool, z_datum: tuple[float, float] | None = None,
                 valve_delay_s: float = 0.5,
                 confirm_next: Callable[[int, int, threading.Event], bool] | None = None,
                 progress=None):
        if not math.isfinite(scan_z_mm) or not math.isfinite(valve_delay_s) or valve_delay_s < 0:
            raise ValueError("scan_z_mm 和 valve_delay_s 必须是有限数，且延时不能为负")
        if z_datum is not None:
            for name, value in zip(("x0", "z0"), z_datum):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"对刀点 {name} 必须是有限数")
        self.servo, self.cnc, self.head = servo, cnc, head
        self.motion = motion
        self.scan_z_mm = scan_z_mm
        #: (x0, z0) read at touch-off. The work height is z0 + (x - x0)*tan(theta):
        #: within a row the needles stay parallel to the tube, so the head has to
        #: follow x or the leading needles dig into the wall.
        self.z_datum = z_datum
        self.operator_confirmed = operator_confirmed
        self.valve_delay_s = valve_delay_s
        self.confirm_next = confirm_next
        self.progress = progress
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

    def _check_ready(self) -> None:
        if not self.operator_confirmed:
            raise RuntimeError("作业前必须由操作员确认 CNC 工作零点并保证行程无障碍")
        for name, device in (("SV630N 伺服", self.servo), ("CNC 三维模组", self.cnc),
                             ("GX28 滑台", self.head)):
            if not device.connected:
                raise RuntimeError(f"{name} 未连接")
        if self.servo.origin_count is None:
            raise RuntimeError("伺服逻辑零位必须在机械靠紧位设置")
        if not self.head.zero_confirmed:
            raise RuntimeError("滑台软件零位必须在物理待机位确认")
        if self.z_datum is None:
            raise RuntimeError("作业前必须通过对刀确认对刀点")
        position = self.cnc.position()
        if position is None or abs(position[2] - self.scan_z_mm) > 0.05:
            raise RuntimeError("作业前 CNC 必须位于已确认的 scan_z_mm 高度")
        if abs(self.head.position_mm() - self.motion.standby_mm) > 0.05:
            raise RuntimeError("滑台不在已确认的 standby_mm 待机位")

    def request_stop(self) -> None:
        """Callable from another thread while a device method is waiting."""
        with self._stop_lock:
            if self.cancel_event.is_set():
                return
            self.cancel_event.set()
            for name, action in (("滑台急停", self.head.stop),
                                 ("CNC 暂停", self.cnc.hold),
                                 ("伺服释放", self.servo.disable)):
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
        self.head.valve("b", False)

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
                self.cnc.move_to(placement.cnc_x_mm, placement.cnc_y_mm,
                                 work_z_mm(plan, placement.cnc_x_mm, self.z_datum))
                self._check_cancel()
                self._cycle_one()
            except BaseException as exc:
                # 落点中途失败或急停：必须留下一行，否则"跑到哪了"无从查起。
                self._notify("placement_finished", row, index, total, placement,
                             ok=False, error=str(exc) or type(exc).__name__)
                raise
            self._completed += 1
            self._notify("placement_finished", row, index, total, placement, ok=True, error=None)
            if index + 1 < total:
                self.state = RunState.WAITING_OPERATOR
                self._check_cancel()
                if not self.confirm_next(index + 1, total, self.cancel_event):
                    raise _StopRequested()
                self._check_cancel()
                self.state = RunState.RUNNING

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
        if total > 1 and self.confirm_next is None:
            raise RuntimeError("剪丝并重新装丝后，每次进入下一落点都需要操作员确认")
        self._check_ready()
        self._completed = 0
        completed_rows = 0
        head = plan.placements[0]
        self.state = RunState.RUNNING
        try:
            self._check_cancel()
            self.servo.move_mm(plan.servo_target_mm)
            self._check_cancel()
            while True:
                self._run_one_row(plan, completed_rows, total)
                completed_rows += 1
                if confirm_next_row is None:
                    break
                # 排间回到安全高度再回排头：自转重复是"一模一样的一排"。
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
