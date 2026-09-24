"""Shared job state machine for hardware and simulated device adapters."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from master.model import HeadMotion
from master.planner import GridPlan


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
    completed_placements: int
    total_placements: int
    error: str | None = None


class _StopRequested(Exception):
    pass


class Coordinator:
    def __init__(self, servo, cnc, head, motion: HeadMotion, *, scan_z_mm: float,
                 operator_confirmed: bool, valve_delay_s: float = 0.5,
                 confirm_next: Callable[[int, int, threading.Event], bool] | None = None):
        if not math.isfinite(scan_z_mm) or not math.isfinite(valve_delay_s) or valve_delay_s < 0:
            raise ValueError("scan_z_mm and valve_delay_s must be finite; delay cannot be negative")
        self.servo, self.cnc, self.head = servo, cnc, head
        self.motion = motion
        self.scan_z_mm = scan_z_mm
        self.operator_confirmed = operator_confirmed
        self.valve_delay_s = valve_delay_s
        self.confirm_next = confirm_next
        self.state = RunState.READY
        self.cancel_event = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_errors: list[str] = []
        for device in (self.servo, self.cnc, self.head):
            device.cancel_event = self.cancel_event

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise _StopRequested()

    def _delay(self) -> None:
        if self.cancel_event.wait(self.valve_delay_s):
            raise _StopRequested()

    def _check_ready(self) -> None:
        if not self.operator_confirmed:
            raise RuntimeError("operator must confirm CNC work zero and clear travel before job")
        for name, device in (("servo", self.servo), ("CNC", self.cnc), ("head", self.head)):
            if not device.connected:
                raise RuntimeError(f"{name} is not connected")
        if self.servo.origin_count is None:
            raise RuntimeError("servo logical zero must be set at the mechanically closed position")
        if not self.head.zero_confirmed:
            raise RuntimeError("head software zero must be confirmed at physical standby")
        position = self.cnc.position()
        if position is None or abs(position[2] - self.scan_z_mm) > 0.05:
            raise RuntimeError("CNC must be at confirmed scan_z_mm before job")
        if abs(self.head.position_mm() - self.motion.standby_mm) > 0.05:
            raise RuntimeError("head is not at confirmed standby_mm")

    def request_stop(self) -> None:
        """Callable from another thread while a device method is waiting."""
        with self._stop_lock:
            if self.cancel_event.is_set():
                return
            self.cancel_event.set()
            for name, action in (("head stop", self.head.stop),
                                 ("CNC hold", self.cnc.hold),
                                 ("servo disable", self.servo.disable)):
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
                errors.append(f"valve {which} close: {exc}")
        return errors

    def _stop_reason(self, reason: str) -> str:
        errors = self._stop_errors + self._close_valves()
        if errors:
            return f"{reason}; safe state unverified: {'; '.join(errors)}"
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

    def run(self, plan: GridPlan) -> RunResult:
        if self.state is not RunState.READY:
            raise RuntimeError("coordinator cannot run twice or resume after stop")
        if len(plan.placements) > 1 and self.confirm_next is None:
            raise RuntimeError("operator must confirm each next placement after cutting and reloading wire")
        self._check_ready()
        completed = 0
        self.state = RunState.RUNNING
        try:
            self._check_cancel()
            self.servo.move_mm(plan.servo_target_mm)
            self._check_cancel()
            for index, placement in enumerate(plan.placements):
                self._check_cancel()
                self.cnc.move_to(placement.cnc_x_mm, placement.cnc_y_mm, self.scan_z_mm)
                self._check_cancel()
                self._cycle_one()
                completed += 1
                if index + 1 < len(plan.placements):
                    self.state = RunState.WAITING_OPERATOR
                    self._check_cancel()
                    if not self.confirm_next(completed, len(plan.placements), self.cancel_event):
                        raise _StopRequested()
                    self._check_cancel()
                    self.state = RunState.RUNNING
            self.state = RunState.COMPLETE
            return RunResult(self.state, completed, len(plan.placements))
        except _StopRequested:
            self.request_stop()
            self.state = RunState.STOPPED
            return RunResult(self.state, completed, len(plan.placements), self._stop_reason("operator stop"))
        except Exception as exc:
            if self.cancel_event.is_set():
                self.state = RunState.STOPPED
                return RunResult(self.state, completed, len(plan.placements), self._stop_reason("operator stop"))
            self.request_stop()
            self.state = RunState.FAULT
            return RunResult(self.state, completed, len(plan.placements), self._stop_reason(str(exc)))
