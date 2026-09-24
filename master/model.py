"""Validated, hardware-independent dimensions and coordinate conventions."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")


def _finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True)
class JobSpec:
    """A half-open rectangular grid: 0 <= x < width, 0 <= y < height."""

    width_mm: float
    height_mm: float
    spacing_mm: float
    plate_thickness_mm: float

    def __post_init__(self) -> None:
        for name in ("width_mm", "height_mm", "spacing_mm", "plate_thickness_mm"):
            _positive(name, getattr(self, name))
        for name in ("width_mm", "height_mm"):
            count = getattr(self, name) / self.spacing_mm
            if not math.isclose(count, round(count), rel_tol=0, abs_tol=1e-9):
                raise ValueError(f"{name} must be an exact multiple of spacing_mm")

    @property
    def x_count(self) -> int:
        return round(self.width_mm / self.spacing_mm)

    @property
    def y_count(self) -> int:
        return round(self.height_mm / self.spacing_mm)

    @property
    def point_count(self) -> int:
        return self.x_count * self.y_count


@dataclass(frozen=True)
class MachineGeometry:
    station_count: int
    station_width_mm: float
    max_expansion_mm: float
    servo_lead_mm: float

    def __post_init__(self) -> None:
        if isinstance(self.station_count, bool) or not isinstance(self.station_count, int) or self.station_count <= 0:
            raise ValueError("station_count must be a positive integer")
        for name in ("station_width_mm", "max_expansion_mm", "servo_lead_mm"):
            _positive(name, getattr(self, name))

    def needle_pitch_mm(self, expansion_mm: float) -> float:
        _finite("expansion_mm", expansion_mm)
        if not 0 <= expansion_mm <= self.max_expansion_mm:
            raise ValueError("expansion_mm outside calibrated range")
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
            raise ValueError("datum_confirmed must be true before motion")
        if self.work_min_mm >= self.work_max_mm:
            raise ValueError("work_min_mm must be below work_max_mm")
        if not self.work_min_mm <= self.standby_mm <= self.work_max_mm:
            raise ValueError("standby_mm outside confirmed work window")
        if not self.work_min_mm <= self.insertion_mm < self.standby_mm:
            raise ValueError("insertion_mm must be below standby_mm and inside confirmed work window")

    @property
    def down_delta_mm(self) -> float:
        return self.insertion_mm - self.standby_mm

    @property
    def up_delta_mm(self) -> float:
        return self.standby_mm - self.insertion_mm
