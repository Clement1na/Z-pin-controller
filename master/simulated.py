"""In-memory device adapters for the same coordinator used by live mode."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimulationRig:
    standby_mm: float
    scan_z_mm: float
    fail_on_first_descent: bool = False
    events: list[tuple] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.servo = _Servo(self)
        self.cnc = _Cnc(self)
        self.head = _Head(self)

    @property
    def valves_closed(self) -> bool:
        return not any(self.head.valves.values())


class _Servo:
    connected = True

    def __init__(self, rig: SimulationRig):
        self.rig = rig
        self.enabled = True
        self.position = 0.0

    def move_mm(self, target: float) -> None:
        self.position = target
        self.rig.events.append(("servo", target))

    def disable(self) -> None:
        self.enabled = False
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

    def hold(self) -> None:
        self.rig.events.append(("cnc_hold",))


class _Head:
    connected = True

    def __init__(self, rig: SimulationRig):
        self.rig = rig
        self.position = rig.standby_mm
        self.valves = {"a": False, "b": False}
        self._failed = False

    def position_mm(self) -> float:
        return self.position

    def move_mm(self, target: float) -> float:
        self.rig.events.append(("head", target))
        if (self.rig.fail_on_first_descent and not self._failed
                and target < self.rig.standby_mm):
            self._failed = True
            raise RuntimeError("injected head descent failure")
        self.position = target
        return target

    def valve(self, which: str, open_: bool) -> None:
        self.valves[which] = open_
        self.rig.events.append(("valve", which, open_))

    def stop(self) -> None:
        self.rig.events.append(("head_stop",))
