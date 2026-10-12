"""Validated, hardware-independent dimensions and coordinate conventions."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Match the firmware's software travel guards; the operator still confirms
# the actual mechanical zero and available travel before motion.
HEAD_WORK_MIN_MM = -15.0
HEAD_WORK_MAX_MM = 15.0
HEAD_PRE_DESCENT_MM = 10.0
DEFAULT_CUT_CLEARANCE_MM = 10.0
MAX_CUT_CLEARANCE_MM = 10.0


def validate_cut_clearance_mm(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= MAX_CUT_CLEARANCE_MM):
        raise ValueError(f"剪丝额外抬升距离必须在 0..{MAX_CUT_CLEARANCE_MM:g} mm 内")
    return float(value)

#: Supported fixture angles, shared by all entry points.
PRESET_ANGLES = (0, 15, 30)
MAX_ANGLE_DEG = 30.0

#: Axial length of the reinforced zone the preset covers. The operator's own
#: wording for it is "20 mm", so it is a constant here rather than an input.
PRESET_ZONE_MM = 20.0

#: Minimum needle pitch -- the servo is left alone and the two loaded needles
#: sit at the mechanical base spacing.
PRESET_PITCH_MM = 10.0

#: Only the two rightmost stations are loaded. The other three stay empty.
PRESET_NEEDLES = 2

#: Densities offered by the menu. The model itself works for any count >= 2;
#: this tuple is only the choice list.
PRESET_COUNTS = (7, 10, 15)


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是一个有限正数")


def _finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须是一个有限数")


@dataclass(frozen=True)
class PresetSpec:
    """One preset row: ``count`` evenly spaced pins across a fixed 20 mm zone.

    Only the two rightmost needles are loaded and they sit at the mechanical
    base pitch of 10 mm, so the servo never moves. A stroke at axial position
    ``u`` therefore implants at ``{u, u + 10}``. For the union of those pairs to
    be a *uniform* grid, ``10 = pairs * spacing`` must hold; taking
    ``pairs = count // 2`` gives ``spacing = 10 / pairs`` and the union covers
    ``{0 .. 2*pairs-1} * spacing``. An odd ``count`` gets one extra stroke at
    ``(count-1) * spacing`` to place the last pin.

    That extra stroke's second needle lands at ``(count-1) * spacing + 10``,
    outside the zone -- the normal overhang case, recorded and cut off later.

    The needles stay vertical; the fixture angle is one of 0, 15 and 30 degrees.
    The tube surface rises towards +x for a positive angle, so the CNC work
    height climbs with x.
    """

    angle_deg: float
    count: int
    depth_mm: float

    def __post_init__(self) -> None:
        _finite("angle_deg", self.angle_deg)
        _positive("depth_mm", self.depth_mm)
        if self.angle_deg not in PRESET_ANGLES:
            raise ValueError("angle_deg 只支持 0、15、30 度")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 2:
            raise ValueError("count 必须是不小于 2 的整数")

    @property
    def pairs(self) -> int:
        """Strokes that place two pins each."""
        return self.count // 2

    @property
    def spacing_mm(self) -> float:
        """Uniform pin spacing, ``10 / pairs``. Derived, never an input."""
        return PRESET_PITCH_MM / self.pairs

    @property
    def length_mm(self) -> float:
        """The reinforced zone. Fixed at 20 mm for the preset."""
        return PRESET_ZONE_MM

    @property
    def x_count(self) -> int:
        """Pin positions along the row. Exactly ``count`` -- never a division.

        ``RowSpec`` derived this as ``floor(length/spacing)``, which can only
        ever come out even here: ``20 / (10/3)`` is exactly ``6.0`` in double
        precision, so the odd preset's ``2*pairs + 1`` was unreachable, and the
        missing pin sat exactly at ``x = 20`` -- the point the half-open
        ``[0, length)`` interval excludes. The count is the input, so it is
        returned directly.
        """
        return self.count

    @property
    def covered_mm(self) -> float:
        """Span actually carrying pins, ``(count-1) * spacing``.

        Equal to 20 mm for odd counts; short of it for even ones -- ``count=10``
        spans only 18 mm because ``spacing = 2.0`` is the sole solution of
        ``10 = integer * spacing``. The operator already said the 20 mm end is
        not required, so that is the expected result, not a defect.
        """
        return (self.count - 1) * self.spacing_mm

    @property
    def tail_mm(self) -> float:
        """Unpinned tail at the far end of the zone, ``length - covered``."""
        return PRESET_ZONE_MM - self.covered_mm

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

        Physical property of the group, not a motion command. The two loaded
        needles are 10 mm apart on a tilted surface, so the right one has to be
        ``10 * tan(theta)`` longer than the left for both tips to reach the wall
        at once. Because the sign follows ``angle_deg``, +15 and -15 need two
        different needle groups.
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
    cut_clearance_mm: float = 0.0

    def __post_init__(self) -> None:
        validate_cut_clearance_mm(self.cut_clearance_mm)
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
        if self.cut_position_mm > self.work_max_mm:
            raise ValueError("剪丝抬升位置超出已确认的滑台工作窗口")

    @property
    def cut_position_mm(self) -> float:
        return self.standby_mm + self.cut_clearance_mm

    @property
    def down_delta_mm(self) -> float:
        return self.insertion_mm - self.standby_mm

    @property
    def up_delta_mm(self) -> float:
        return self.standby_mm - self.insertion_mm
