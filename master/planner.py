"""Pure five-head raster planner. No device access is permitted here."""

from __future__ import annotations

import math
from dataclasses import dataclass

from master.model import JobSpec, MachineGeometry


@dataclass(frozen=True)
class Placement:
    cnc_x_mm: float
    cnc_y_mm: float
    needle_hits: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class GridPlan:
    needle_pitch_mm: float
    servo_target_mm: float
    placements: tuple[Placement, ...]


def plan_grid(job: JobSpec, machine: MachineGeometry) -> GridPlan:
    """Cover a half-open grid using full, simultaneous head strokes only.

    A block spans ``station_count * multiplier`` grid points. Repeating the
    needle array at adjacent grid offsets fills that block exactly once.
    """
    spacing = job.spacing_mm
    min_step = math.ceil(machine.station_width_mm / spacing - 1e-9)
    max_step = math.floor((machine.station_width_mm + machine.max_expansion_mm) / spacing + 1e-9)
    candidates: list[tuple[int, float]] = []
    for step in range(max(1, min_step), max_step + 1):
        pitch = step * spacing
        expansion = pitch - machine.station_width_mm
        if -1e-9 <= expansion <= machine.max_expansion_mm + 1e-9:
            candidates.append((step, pitch))
    if not candidates:
        raise ValueError("No calibrated needle pitch is an integer multiple of spacing_mm")

    full_rows = [(step, pitch) for step, pitch in candidates
                 if job.x_count % (machine.station_count * step) == 0]
    if not full_rows:
        raise ValueError("X edge requires a partial five-head stroke; full-head plan would exceed the field")
    step, pitch = min(full_rows, key=lambda item: (abs(item[1] - 12.0), item[1]))
    expansion = pitch - machine.station_width_mm
    placements: list[Placement] = []
    block_size = machine.station_count * step
    for row in range(job.y_count):
        y = row * spacing
        for block in range(0, job.x_count, block_size):
            for offset in range(step):
                base = (block + offset) * spacing
                hits = tuple((base + station * pitch, y)
                             for station in range(machine.station_count))
                placements.append(Placement(base, y, hits))
    return GridPlan(pitch, machine.servo_target_mm(expansion), tuple(placements))
