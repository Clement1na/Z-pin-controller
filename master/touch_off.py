"""Persistent, opt-in touch-off geometry; no device connections are opened here."""
from __future__ import annotations

import hashlib
import json
import math
import threading

from master.model import HEAD_PRE_DESCENT_MM, HEAD_WORK_MIN_MM, HEAD_WORK_MAX_MM, HeadMotion
from master.planner import highest_work_z_mm, plan_at_x

SECTION = "saved_touch_off"


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"保存的对刀点 {label} 必须是有限数")
    return float(value)


def _context(cfg):
    values = [(section, key, cfg.get(section, {}).get(key, default))
              for section, key, default in (
                  ("cnc3d", "port", ""), ("cnc3d", "baud", 115200),
                  ("head", "port", ""), ("head", "baud", 115200),
                  ("servo", "adapter", ""), ("servo", "lead_mm", 13.3333333),
                  ("servo", "counts_per_rev", 262144))]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def make_record(cfg, row, xyz, motion, scan_z):
    return {"version": 1, "context": _context(cfg), "angle_deg": row.angle_deg,
            "x_mm": xyz[0], "y_mm": xyz[1], "z_mm": xyz[2], "scan_z_mm": scan_z,
            "standby_mm": motion.standby_mm, "cut_clearance_mm": motion.cut_clearance_mm,
            "work_min_mm": motion.work_min_mm, "work_max_mm": motion.work_max_mm}


def read_record(cfg, row, plan, *, head_clearance_travel=False):
    record = cfg.get(SECTION)
    if record is None:
        return None
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
        raise ValueError("保存的对刀点格式不支持，请重新对刀")
    if record.get("context") != _context(cfg):
        raise ValueError("接口或标定参数已变化，请重新对刀")
    angle = _finite(record.get("angle_deg"), "架角")
    if angle != row.angle_deg:
        raise ValueError("架角已变化，请重新对刀并核对针组")
    xyz = tuple(_finite(record.get(key), key) for key in ("x_mm", "y_mm", "z_mm"))
    scan_z = _finite(record.get("scan_z_mm"), "排间安全Z")
    standby = _finite(record.get("standby_mm"), "滑台对刀位")
    if standby != -HEAD_PRE_DESCENT_MM:
        raise ValueError("滑台准备位置已变化，请重新对刀")
    motion = HeadMotion(standby, standby-row.slide_travel_mm,
                        _finite(record.get("work_min_mm"), "工作下界"),
                        _finite(record.get("work_max_mm"), "工作上界"), True,
                        cut_clearance_mm=_finite(record.get("cut_clearance_mm"), "剪丝距离"))
    if not HEAD_WORK_MIN_MM <= motion.work_min_mm <= 0 <= motion.work_max_mm <= HEAD_WORK_MAX_MM:
        raise ValueError("保存的滑台工作窗口无效，请重新对刀")
    if head_clearance_travel and motion.cut_clearance_mm != HEAD_PRE_DESCENT_MM:
        raise ValueError("用户版需要15 mm剪丝抬升，请重新对刀")
    if not head_clearance_travel and scan_z+1e-9 < highest_work_z_mm(plan_at_x(plan, xyz[0]), (xyz[0], xyz[2])):
        raise ValueError("保存的安全Z低于本排工作高度，请重新对刀")
    return xyz, motion, scan_z


def restore_position(cnc, head, xyz, motion, scan_z, *, destination_z=None):
    """Stay at confirmed head zero until CNC has reached the saved safe position."""
    position = _finite(head.position_mm(), "当前滑台位置")
    if destination_z is not None:
        destination_z = _finite(destination_z, "目标工作Z")
    if not head.zero_confirmed or abs(position) > 0.05:
        raise RuntimeError("复用对刀点前必须确认 GX28 在本次人工零位0 mm")
    events = [getattr(device, "cancel_event", None) for device in (cnc, head)]
    events = [event for event in events if event is not None]
    done = threading.Event()
    stopped = threading.Event()
    stop_lock = threading.Lock()
    stop_errors = []

    def cancelled():
        return any(event.is_set() for event in events)

    def halt():
        with stop_lock:
            if stopped.is_set():
                return
            stopped.set()
        for device, method in ((cnc, "hold"), (head, "stop")):
            try:
                getattr(device, method)()
            except Exception as exc:
                stop_errors.append(f"{method}: {exc}")

    def watch():
        while not done.wait(0.01):
            if cancelled():
                halt()
                return

    def check_cancel():
        if cancelled():
            raise RuntimeError("已取消对刀点恢复，未继续移动")

    monitor = threading.Thread(target=watch, daemon=True) if events else None
    if monitor is not None:
        monitor.start()
    failure = None
    try:
        check_cancel()
        _restore_position(cnc, head, xyz, motion, scan_z, check_cancel,
                          destination_z=destination_z)
        check_cancel()
    except BaseException as exc:
        failure = exc
        halt()
    finally:
        done.set()
        if monitor is not None:
            monitor.join()
    if failure is not None:
        if stop_errors:
            raise RuntimeError(f"{failure}；停止状态未确认：{'; '.join(stop_errors)}") from failure
        raise failure


def _restore_position(cnc, head, xyz, motion, scan_z, check_cancel, *, destination_z=None):
    verify = getattr(head, "verify_work_window", None)
    if verify is not None:
        verify(motion)
    head.valve("a", False)
    head.valve("b", False)
    def position_head():
        check_cancel()
        head.motion = motion
        head.move_mm(motion.cut_position_mm)
        actual_head = head.position_mm()
        if (not head.zero_confirmed or not math.isfinite(actual_head)
                or abs(actual_head-motion.cut_position_mm) > 0.05):
            raise RuntimeError("恢复 GX28 剪丝安全位回读不一致")
    if destination_z is not None:
        position_head()
    current = cnc.position()
    if current is None or len(current) != 3:
        raise RuntimeError("读不到 CNC 工作坐标，未恢复已保存对刀点")
    current = tuple(_finite(value, "当前CNC位置") for value in current)
    travel_z = max(current[2], scan_z)
    check_cancel()
    cnc.move_to(current[0], current[1], travel_z)
    check_cancel()
    cnc.move_to(xyz[0], xyz[1], travel_z)
    if travel_z != scan_z:
        check_cancel()
        cnc.move_to(xyz[0], xyz[1], scan_z)
    actual = cnc.position()
    if actual is None or len(actual) != 3 or not all(math.isfinite(v) and abs(v-target) <= 0.05
                                 for v, target in zip(actual, (xyz[0], xyz[1], scan_z))):
        raise RuntimeError("恢复对刀点的 CNC 安全位置回读不一致")
    check_cancel()
    if destination_z is None:
        position_head()
    else:
        cnc.move_to(xyz[0], xyz[1], destination_z)
        check_cancel()
        actual = cnc.position()
        if actual is None or len(actual) != 3 or not all(math.isfinite(v) and abs(v-target) <= 0.05
                for v, target in zip(actual, (xyz[0], xyz[1], destination_z))):
            raise RuntimeError("恢复对刀工作位置回读不一致")
