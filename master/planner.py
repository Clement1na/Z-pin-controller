"""Pure five-head row planner. No device access is permitted here."""

from __future__ import annotations

import math
from dataclasses import dataclass

from master.model import MachineGeometry, RowSpec


@dataclass(frozen=True)
class Placement:
    """One simultaneous five-head stroke. The CNC work height is not stored here:
    it depends on the datum read at touch-off, which happens after planning."""

    cnc_x_mm: float
    cnc_y_mm: float
    needle_hits: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class RowPlan:
    needle_pitch_mm: float
    servo_target_mm: float
    slope: float
    placements: tuple[Placement, ...]


def _choose_pitch(row: RowSpec, machine: MachineGeometry) -> tuple[int, float]:
    """Pick the calibrated pitch closest to 12 mm, as a whole number of grid steps."""
    spacing = row.spacing_mm
    min_step = math.ceil(machine.station_width_mm / spacing - 1e-9)
    max_step = math.floor((machine.station_width_mm + machine.max_expansion_mm) / spacing + 1e-9)
    candidates: list[tuple[int, float]] = []
    for step in range(max(1, min_step), max_step + 1):
        pitch = step * spacing
        expansion = pitch - machine.station_width_mm
        if -1e-9 <= expansion <= machine.max_expansion_mm + 1e-9:
            candidates.append((step, pitch))
    if not candidates:
        raise ValueError("标定的针距里没有任何一个能整除 spacing_mm")
    return min(candidates, key=lambda item: (abs(item[1] - 12.0), item[1]))


def plan_row(row: RowSpec, machine: MachineGeometry) -> RowPlan:
    """Cover one reinforced row with simultaneous five-head strokes.

    A block spans ``station_count * step`` grid points. Repeating the needle array
    at adjacent grid offsets fills that block exactly once. The last block is
    allowed to be **partial**: the needle array is 40-80 mm wide while the
    reinforced zone is ~20 mm, so overhanging needles are the normal case. They
    still meet the tube wall (the real stock is longer) and the overhang is cut
    off afterwards, so a short final block is not an error.
    """
    step, pitch = _choose_pitch(row, machine)
    expansion = pitch - machine.station_width_mm
    spacing = row.spacing_mm
    placements: list[Placement] = []
    block_size = machine.station_count * step
    for block in range(0, row.x_count, block_size):
        for offset in range(step):
            index = block + offset
            if index >= row.x_count:
                continue
            base = index * spacing
            hits = tuple((base + station * pitch, 0.0)
                         for station in range(machine.station_count))
            placements.append(Placement(base, 0.0, hits))
    return RowPlan(pitch, machine.servo_target_mm(expansion), row.slope, tuple(placements))


def work_z_mm(plan: RowPlan, cnc_x_mm: float, datum: tuple[float, float]) -> float:
    """CNC work height at ``cnc_x_mm``: ``z = z0 + (x - x0)*tan(theta)``.

    The needles form a line parallel to the tube surface, so the gap between every
    needle tip and the wall is the same -- but only when the head sits at the
    height this function returns. Walking the head along x without moving z would
    drive the leading needles into the wall.
    """
    x0, z0 = datum
    return z0 + (cnc_x_mm - x0) * plan.slope


def highest_work_z_mm(plan: RowPlan, datum: tuple[float, float]) -> float:
    """Tallest work height anywhere in the row.

    The row's safety height must clear this. Because ``slope`` is signed, the
    tallest point is the **last** placement for a positive angle and the **first**
    one for a negative angle -- never assume monotonicity, or "lift to safety
    height" becomes a plunge into the tube when the angle is negative.
    """
    if not plan.placements:
        raise ValueError("排程结果为空，没有任何落点")
    return max(work_z_mm(plan, placement.cnc_x_mm, datum) for placement in plan.placements)
