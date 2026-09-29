"""In-memory device adapters for the same coordinator used by live mode."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimulationRig:
    standby_mm: float
    scan_z_mm: float
    fail_on_first_descent: bool = False
    events: list[tuple] = field(default_factory=list)
    #: Row geometry, so the rig can tell a correct Z profile from a wrong one.
    slope: float = 0.0
    datum: tuple[float, float] = (0.0, 0.0)
    geometry_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.servo = _Servo(self)
        self.cnc = _Cnc(self)
        self.head = _Head(self)

    @property
    def valves_closed(self) -> bool:
        return not any(self.head.valves.values())

    def check_geometry(self, x: float, z: float) -> None:
        """Every CNC move must land on the work-height line or the clearance height.

        Without this the smoke test would happily pass a coordinator that holds Z
        constant -- which is exactly the bug that digs the leading needles into a
        tilted tube.
        """
        expected = self.datum[1] + (x - self.datum[0]) * self.slope
        if abs(z - expected) <= 1e-6 or abs(z - self.scan_z_mm) <= 1e-6:
            return
        self.geometry_errors.append(
            f"CNC 在 x={x:.4f} 处的 z={z:.4f} 既不等于工作高度 z(x)={expected:.4f}，"
            f"也不等于排间安全高度 {self.scan_z_mm:.4f}")


class _Servo:
    connected = True

    def __init__(self, rig: SimulationRig):
        self.rig = rig
        self.enabled = False
        self.position = 0.0
        self.boot_position = 0.0
        self.origin_count = None

    @property
    def setup_offset_mm(self) -> float:
        return self.position - self.boot_position

    def enable(self) -> None:
        self.enabled = True
        self.rig.events.append(("servo_enable",))

    def jog_setup_mm(self, delta: float) -> None:
        if not self.enabled or self.origin_count is not None or not 0 < abs(delta) <= 0.5:
            raise RuntimeError("点动参数不合法")
        self.position += delta
        self.rig.events.append(("servo_setup_jog", delta))

    def set_zero(self) -> None:
        if not self.enabled:
            raise RuntimeError("设零前必须先使能伺服")
        self.origin_count = self.position
        self.rig.events.append(("servo_zero",))

    def move_mm(self, target: float) -> None:
        if self.origin_count is None:
            raise RuntimeError("伺服零位缺失")
        self.position = self.origin_count + target
        self.rig.events.append(("servo", target))

    def disable(self) -> None:
        self.enabled = False
        self.origin_count = None
        self.rig.events.append(("servo_disable",))


class _Cnc:
    connected = True

    def __init__(self, rig: SimulationRig):
        self.rig = rig
        self.xyz = (0.0, 0.0, rig.scan_z_mm)

    def position(self) -> tuple[float, float, float]:
        return self.xyz

    def move_to(self, x: float, y: float, z: float) -> None:
        self.xyz = (x, y, z)
        self.rig.events.append(("cnc", x, y, z))
        self.rig.check_geometry(x, z)

    def hold(self) -> None:
        self.rig.events.append(("cnc_hold",))


class _Head:
    connected = True

    def __init__(self, rig: SimulationRig):
        self.rig = rig
        self.position = rig.standby_mm
        self.valves = {"a": False, "b": False}
        self._failed = False
        self.zero_confirmed = False

    def zero(self) -> None:
        self.position = 0.0
        self.zero_confirmed = True
        self.rig.events.append(("head_zero",))

    def position_mm(self) -> float:
        return self.position

    def move_mm(self, target: float) -> float:
        if not self.zero_confirmed:
            raise RuntimeError("滑台零位缺失")
        self.rig.events.append(("head", target))
        if (self.rig.fail_on_first_descent and not self._failed
                and target < self.rig.standby_mm):
            self._failed = True
            raise RuntimeError("注入的滑台下行故障")
        self.position = target
        return target

    def valve(self, which: str, open_: bool) -> None:
        self.valves[which] = open_
        self.rig.events.append(("valve", which, open_))

    def stop(self) -> None:
        self.zero_confirmed = False
        self.rig.events.append(("head_stop",))
