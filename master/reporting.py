# -*- coding: utf-8 -*-
"""逐落点结果记录：一份 CSV 明细 + 一份 JSON 摘要。

设计目标是"跑到哪了"必须有据可查：每完成一个落点立刻落盘并 flush，
所以断电或急停之后 CSV 里已经有全部已完成的落点，不会只剩一行总结。
记录器本身绝不打断机器——写不进去只记进 errors，运行继续。
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

FIELDS = ("index", "cnc_x_mm", "cnc_y_mm", "needle_hits", "status", "duration_s", "error")


class RunReporter:
    def __init__(self, *, job, plan, machine, motion, scan_z_mm: float, out_dir: Path,
                 stamp: datetime | None = None):
        self.job, self.plan, self.machine, self.motion = job, plan, machine, motion
        self.scan_z_mm = scan_z_mm
        self.errors: list[str] = []
        self.out_dir = Path(out_dir)
        self.stamp = stamp or datetime.now()
        self.csv_path = self.out_dir / f"run-{self.stamp:%Y%m%d-%H%M%S}.csv"
        self.json_path = self.out_dir / f"run-{self.stamp:%Y%m%d-%H%M%S}.json"
        self._stream = None
        self._writer = None
        self._started: dict[int, float] = {}
        self._artifacts: tuple[Path, Path] | None = None
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._stream = self.csv_path.open("w", newline="", encoding="utf-8-sig")
            self._writer = csv.DictWriter(self._stream, fieldnames=FIELDS)
            self._writer.writeheader()
            self._stream.flush()
        except OSError as exc:
            self._fail(f"结果文件不可写，本次运行不记录：{exc}")

    # ---- coordinator 进度回调 ----
    def placement_started(self, index: int, total: int, placement) -> None:
        self._started[index] = time.monotonic()

    def placement_finished(self, index: int, total: int, placement, *,
                           ok: bool, error: str | None) -> None:
        if self._writer is None:
            return
        started = self._started.pop(index, None)
        duration = "" if started is None else f"{time.monotonic() - started:.3f}"
        try:
            self._writer.writerow({
                "index": index + 1,
                "cnc_x_mm": f"{placement.cnc_x_mm:.4f}",
                "cnc_y_mm": f"{placement.cnc_y_mm:.4f}",
                "needle_hits": ";".join(f"{x:g},{y:g}" for x, y in placement.needle_hits),
                "status": "ok" if ok else "incomplete",
                "duration_s": duration,
                "error": error or "",
            })
            # 每个落点一次 flush：约每分钟一次，代价可忽略，换来断电后不丢已完成落点。
            self._stream.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"写结果行失败，后续落点不再记录：{exc}")

    # ---- 收尾 ----
    def close(self) -> None:
        """关掉 CSV 句柄，不写摘要。"""
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError as exc:
                self.errors.append(f"关闭 CSV 失败：{exc}")
        self._stream = None
        self._writer = None

    def finish(self, result, *, warnings=()) -> tuple[Path, Path] | None:
        """写出 JSON 摘要。CSV 已在每个落点时落盘，这里只负责收口。"""
        if self._artifacts is not None:
            return self._artifacts
        if self._stream is None:
            return None
        self.close()
        summary = {
            "state": result.state.name,
            "completed_placements": result.completed_placements,
            "total_placements": result.total_placements,
            "error": result.error,
            "started_at": self.stamp.isoformat(),
            "finished_at": datetime.now().isoformat(),
            "scan_z_mm": self.scan_z_mm,
            "job": asdict(self.job),
            "machine": asdict(self.machine),
            "motion": asdict(self.motion),
            "plan": {"needle_pitch_mm": self.plan.needle_pitch_mm,
                     "servo_target_mm": self.plan.servo_target_mm,
                     "placement_count": len(self.plan.placements)},
            "csv": self.csv_path.name,
            "warnings": list(warnings),
            "recording_errors": list(self.errors),
        }
        try:
            tmp = self.json_path.with_name(self.json_path.name + ".tmp")
            tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.json_path)
        except OSError as exc:
            self._fail(f"写摘要失败：{exc}")
            return None
        self._artifacts = (self.csv_path, self.json_path)
        return self._artifacts

    def _fail(self, message: str) -> None:
        self.errors.append(message)
        self.close()
