"""Pure row planner. No device access is permitted here."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from master.model import PRESET_NEEDLES, PRESET_PITCH_MM, MachineGeometry, PresetSpec


@dataclass(frozen=True)
class Placement:
    """One simultaneous stroke of the loaded needle group. The CNC work height is
    not stored here: it depends on the datum read at touch-off, which happens
    after planning."""

    cnc_x_mm: float
    cnc_y_mm: float
    needle_hits: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class RowPlan:
    needle_pitch_mm: float
    servo_target_mm: float
    slope: float
    placements: tuple[Placement, ...]


def _hits(base_mm: float, pitch_mm: float) -> tuple[tuple[float, float], ...]:
    """The loaded needles for one stroke, at ``base + station * pitch``.

    Only ``PRESET_NEEDLES`` stations carry a needle. Walking
    ``machine.station_count`` here would invent hits for the three empty
    stations and hand the operator a pin count that is not on the machine.
    """
    return tuple((base_mm + station * pitch_mm, 0.0) for station in range(PRESET_NEEDLES))


def plan_preset_row(spec: PresetSpec, machine: MachineGeometry) -> RowPlan:
    """Cover the preset's fixed 20 mm zone with simultaneous two-needle strokes.

    The pitch is the mechanical base spacing, so the servo target is exactly 0
    and the servo never moves. ``spec.spacing_mm`` is derived from the pin count
    (``10 / pairs``), which is what makes the union of the strokes a uniform
    grid; an odd count gets one extra stroke for the last pin, whose second
    needle overhangs the zone. That overhang is recorded like any other -- the
    stock is longer and it is cut off afterwards.
    """
    if PRESET_NEEDLES > machine.station_count:
        raise ValueError(
            f"预设需要 {PRESET_NEEDLES} 根针，但机器只有 {machine.station_count} 个工位"
        )
    if abs(machine.station_width_mm - PRESET_PITCH_MM) > 1e-9:
        raise ValueError("预设要求机械基准针距为 10 mm，请核对机器标定")
    pitch = machine.station_width_mm
    spacing = spec.spacing_mm
    placements: list[Placement] = []
    for index in range(spec.pairs):
        base = index * spacing
        placements.append(Placement(base, 0.0, _hits(base, pitch)))
    if spec.count % 2:
        base = (spec.count - 1) * spacing
        placements.append(Placement(base, 0.0, _hits(base, pitch)))
    # 针距锁在机械基准间距，伺服不动 —— 目标就是 0，不是算出来的 -0.0。
    return RowPlan(pitch, 0.0, spec.slope, tuple(placements))


def work_z_mm(plan: RowPlan, cnc_x_mm: float, datum: tuple[float, float]) -> float:
    """CNC work height at ``cnc_x_mm``: ``z = z0 + (x - x0)*tan(theta)``.

    The needles form a line parallel to the tube surface, so the gap between every
    needle tip and the wall is the same -- but only when the head sits at the
    height this function returns. Walking the head along x without moving z would
    drive the leading needles into the wall.
    """
    x0, z0 = datum
    return z0 + (cnc_x_mm - x0) * plan.slope


def plan_at_y(plan: RowPlan, y_mm: float) -> RowPlan:
    """Bind relative row Y coordinates to this setup's measured touch-off Y."""
    if isinstance(y_mm, bool) or not isinstance(y_mm, (int, float)) or not math.isfinite(y_mm):
        raise ValueError("对刀 Y 必须是有限数")
    return replace(plan, placements=tuple(
        replace(p, cnc_y_mm=p.cnc_y_mm + y_mm,
                needle_hits=tuple((x, y + y_mm) for x, y in p.needle_hits))
        for p in plan.placements))


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
