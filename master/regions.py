"""Tube-length geometry, independent of the loaded needle arrangement."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from master.model import PRESET_ANGLES, PRESET_ZONE_MM, PresetSpec
from master.planner import plan_preset_row, plan_at_x


@dataclass(frozen=True)
class TubeLayout:
    """Keep each original X=20 mm region; measure only the blank gap along tube."""

    gap_mm: float

    def __post_init__(self):
        if (isinstance(self.gap_mm, bool) or not isinstance(self.gap_mm, (int, float))
                or not math.isfinite(self.gap_mm) or self.gap_mm < 0):
            raise ValueError('工作区之间的沿管空白间距必须是有限且非负的毫米数')
        if not math.isfinite(self.span_mm(30)):
            raise ValueError('三个工作区总长度超出可表示范围，请减小空白间距')

    def span_mm(self, angle_deg):
        return 3*PRESET_ZONE_MM/math.cos(self._radians(angle_deg)) + 2*self.gap_mm

    @staticmethod
    def _radians(angle_deg):
        if isinstance(angle_deg, bool) or angle_deg not in PRESET_ANGLES:
            raise ValueError('架角只支持0、15、30度')
        return math.radians(angle_deg)

    def offset_xz(self, region_index, angle_deg):
        if type(region_index) is not int or not 0 <= region_index < 3:
            raise ValueError('工作区序号必须为0、1或2')
        radians = self._radians(angle_deg)
        x = region_index*(PRESET_ZONE_MM+self.gap_mm*math.cos(radians))
        return x, x*math.tan(radians)

    def as_config(self):
        return {'gap_mm': self.gap_mm}

    @classmethod
    def from_config(cls, section):
        if not isinstance(section, dict) or 'gap_mm' not in section:
            raise ValueError('请填写工作区之间的沿管空白间距')
        return cls(gap_mm=section['gap_mm'])


@dataclass(frozen=True)
class TubeJobParameters:
    row: PresetSpec
    layout: TubeLayout


class ThreeRegionJobSource:
    """Flatten three unchanged preset regions into one turnable row."""

    def __init__(self, layout: TubeLayout):
        self.layout = layout

    def build(self, cfg, overrides):
        from master.master import PresetJobSource
        if TubeLayout.from_config(cfg.get('regions')) != self.layout:
            raise ValueError('工作区间距配置已变化，未开始植入；请重新核对任务')
        return PresetJobSource().build(cfg, overrides)

    def plan(self, job, machine):
        single = plan_preset_row(job, machine)
        placements = []
        for index in range(3):
            dx, _ = self.layout.offset_xz(index, job.angle_deg)
            placements.extend(plan_at_x(single, dx).placements)
        if not all(math.isfinite(p.cnc_x_mm) and
                   all(math.isfinite(x) for x, _ in p.needle_hits) for p in placements):
            raise ValueError('工作区坐标超出可表示范围，请减小空白间距')
        return replace(single, placements=tuple(placements),
                       region_counts=(len(single.placements),)*3,
                       region_gap_mm=self.layout.gap_mm)
