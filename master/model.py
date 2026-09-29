"""Validated, hardware-independent dimensions and coordinate conventions."""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Hard process limit. The carbon wall is 2 mm thick and the metal joint sits
#: directly below it, so a mistyped depth is a collision, not a bad sample.
MAX_DEPTH_MM = 3.0

#: Sanity bound on the fixturing angle; the process only uses 0/+/-15/+/-30.
MAX_ANGLE_DEG = 45.0


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是一个有限正数")


def _finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须是一个有限数")


@dataclass(frozen=True)
class RowSpec:
    """One reinforced row on a tilted tube.

    The needles stay vertical; the tube is fixtured at ``angle_deg``. That is how
    +/-15 and +/-30 are achieved. ``angle_deg`` is **signed**: a positive angle
    means the tube surface rises towards +x, so the work height tracked by the CNC
    climbs with x. A negative angle is the mirror case and walks the work height
    back down -- the two signs are genuinely different machine situations.

    ``length_mm`` is the **reinforced zone** the operator asked for, not the stock
    length. The pins are spaced ``spacing_mm`` apart starting at 0, so the span
    that actually carries pins is ``covered_mm`` <= ``length_mm``; see ``x_count``
    for why the two differ. The needle pitch is chosen from the calibrated 10-20 mm
    band, so most of the five needles land outside this span either way. That is
    expected: the real stock is longer and the overhang is cut off afterwards.
    """

    angle_deg: float
    length_mm: float
    spacing_mm: float
    depth_mm: float

    def __post_init__(self) -> None:
        _finite("angle_deg", self.angle_deg)
        for name in ("length_mm", "spacing_mm", "depth_mm"):
            _positive(name, getattr(self, name))
        if abs(self.angle_deg) > MAX_ANGLE_DEG:
            raise ValueError(f"angle_deg 必须在 ±{MAX_ANGLE_DEG:g} 度以内")
        if self.depth_mm > MAX_DEPTH_MM:
            raise ValueError(
                f"depth_mm 沿垂直管面量，不得超过 {MAX_DEPTH_MM:g} mm；"
                f"碳管壁下方就是金属接头"
            )
        if self.length_mm < self.spacing_mm:
            raise ValueError(
                f"length_mm（{self.length_mm:g}）比 spacing_mm（{self.spacing_mm:g}）"
                f"还短，一个点都放不下"
            )

    @property
    def x_count(self) -> int:
        """Pin positions along the row: ``floor(length/spacing)``.

        The reinforced zone is the real span the operator asked for; the pitch
        is what the process dictates. They rarely divide evenly -- the sample
        plan's own numbers are 20 mm at 2.75 mm (7 positions) and 20 mm at
        1.30 mm (15 positions), neither of them exact. Rounding *up* would put
        a pin past the zone, so the count is the number that fits, and the
        short tail at the end of the row simply gets no pin. The wizard echoes
        that tail so the operator can see it rather than discover it.
        """
        return math.floor(self.length_mm / self.spacing_mm)

    @property
    def covered_mm(self) -> float:
        """Span actually carrying pins, ``x_count * spacing_mm``. Never more
        than ``length_mm``; the difference is the unpinned tail."""
        return self.x_count * self.spacing_mm

    @property
    def slope(self) -> float:
        """``tan(theta)``, signed: positive means the surface rises towards +x."""
        return math.tan(math.radians(self.angle_deg))

    @property
    def slide_travel_mm(self) -> float:
        """GX28 slide travel. Only ``|angle_deg|`` matters: the depth is measured
        perpendicular to the tube surface, so the vertical stroke is ``depth/cos``."""
        return self.depth_mm / math.cos(math.radians(self.angle_deg))

    def needle_length_step_mm(self, pitch_mm: float) -> float:
        """Arithmetic needle-length step of the needle group, ``pitch * tan(theta)``.

        Physical property of the group, not a motion command: because the sign
        follows ``angle_deg``, +15 and -15 need two different needle groups.
        """
        _finite("pitch_mm", pitch_mm)
        if pitch_mm <= 0:
            raise ValueError("pitch_mm 必须是一个有限正数")
        return pitch_mm * self.slope


@dataclass(frozen=True)
class MachineGeometry:
    station_count: int
    station_width_mm: float
    max_expansion_mm: float
    servo_lead_mm: float

    def __post_init__(self) -> None:
        if isinstance(self.station_count, bool) or not isinstance(self.station_count, int) or self.station_count <= 0:
            raise ValueError("station_count 必须是正整数")
        for name in ("station_width_mm", "max_expansion_mm", "servo_lead_mm"):
            _positive(name, getattr(self, name))

    def needle_pitch_mm(self, expansion_mm: float) -> float:
        _finite("expansion_mm", expansion_mm)
        if not 0 <= expansion_mm <= self.max_expansion_mm:
            raise ValueError("expansion_mm 超出标定范围")
        return self.station_width_mm + expansion_mm

    def servo_target_mm(self, expansion_mm: float) -> float:
        self.needle_pitch_mm(expansion_mm)
        return -expansion_mm


@dataclass(frozen=True)
class HeadMotion:
    """GX28 software coordinates; negative delta is the observed descent."""

    standby_mm: float
    insertion_mm: float
    work_min_mm: float
    work_max_mm: float
    datum_confirmed: bool

    def __post_init__(self) -> None:
        for name in ("standby_mm", "insertion_mm", "work_min_mm", "work_max_mm"):
            _finite(name, getattr(self, name))
        if not self.datum_confirmed:
            raise ValueError("动作前 datum_confirmed 必须为 true")
        if self.work_min_mm >= self.work_max_mm:
            raise ValueError("work_min_mm 必须小于 work_max_mm")
        if not self.work_min_mm <= self.standby_mm <= self.work_max_mm:
            raise ValueError("standby_mm 不在已确认的工作窗口内")
        if not self.work_min_mm <= self.insertion_mm < self.standby_mm:
            raise ValueError("insertion_mm 必须小于 standby_mm，且在已确认的工作窗口内")

    @property
    def down_delta_mm(self) -> float:
        return self.insertion_mm - self.standby_mm

    @property
    def up_delta_mm(self) -> float:
        return self.standby_mm - self.insertion_mm
