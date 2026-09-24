# -*- coding: utf-8 -*-
"""三个执行器的统一封装：
  ServoAxis  —— 变距模组（SV630N 伺服, EtherCAT, 复用 sv630n_pc.py 的类）
  Cnc3D      —— 三维模组 X/Y/Z（ESP32 Grbl/FluidNC, 串口, 复用 cnc3d 库）
  InsertHead —— 植入滑台 GX28 + 双气阀（ESP32 自研协议, 见 firmware/insert_head/insert_head.ino）
对外全部用 mm 单位，count 换算只在 ServoAxis 内部做。
"""
from __future__ import annotations

import re
import sys
import threading
import time
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "servo") not in sys.path:
    sys.path.insert(0, str(ROOT / "servo"))            # 让 sv630n_pc 可导入
sys.path.insert(0, str(ROOT / "cnc3d" / "src"))        # 让 cnc_grbl 可导入


class ServoAxis:
    """变距模组。mm 接口，内部换算 count（18bit 编码器 262144/圈）。"""

    def __init__(self, adapter: str = "", lead_mm: float = 13.3333333,
                 counts_per_rev: int = 262144, vel_mm_s: float = 1.0,
                 max_expansion_mm: float = 10.0, timeout_s: float = 5.0,
                 device_factory=None):
        self.adapter = adapter
        self.lead_mm = lead_mm
        self.cpr = counts_per_rev
        self.vel_mm_s = vel_mm_s
        self.max_expansion_mm = max_expansion_mm
        self.timeout_s = timeout_s
        self.device_factory = device_factory
        self.cancel_event = None
        self.origin_count = None
        self.boot_count = None
        self.dev = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def connect(self) -> None:
        if self.connected:
            return
        if not self.adapter:
            raise ValueError("EtherCAT adapter must be selected explicitly")
        if self.device_factory is None:
            from sv630n_pc import LEAD_MM, Sv630n
            if not math.isclose(self.lead_mm, LEAD_MM, rel_tol=1e-6):
                raise ValueError("servo lead_mm differs from calibrated LEAD_MM")
            factory = Sv630n
        else:
            factory = self.device_factory
        dev = factory(self.adapter)
        try:
            dev.open()
            dev.running = True
            threading.Thread(target=dev.cyclic, daemon=True).start()
            self._wait_link(dev)
        except Exception:
            dev.close()
            raise
        self.dev = dev
        self.origin_count = None
        self.boot_count = dev.status["apos"]

    def _wait_link(self, dev) -> None:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            status = dev.status
            if status["sw"] & 0x0008:
                raise RuntimeError("servo fault on connect")
            if status["wkc_ok"]:
                return
            time.sleep(0.01)
        raise TimeoutError("EtherCAT working counter did not become valid")

    def _wait_ready(self, dev) -> None:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("servo wait cancelled")
            status = dev.status
            if status["sw"] & 0x0008:
                raise RuntimeError("servo fault before motion")
            if status["wkc_ok"] and status["sw"] & 0x006F == 0x0027:
                return
            time.sleep(0.01)
        if not dev.status["wkc_ok"]:
            raise TimeoutError("EtherCAT working counter did not become valid")
        raise TimeoutError("servo did not reach operation enabled state")

    def close(self) -> None:
        self.origin_count = None
        self.boot_count = None
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def _mm2count(self, mm: float) -> int:
        return int(round(mm / self.lead_mm * self.cpr))

    def _count2mm(self, count: float) -> float:
        return count / self.cpr * self.lead_mm

    @property
    def position_mm(self) -> float:
        if not self.connected:
            raise RuntimeError("伺服未连接")
        if self.origin_count is None:
            raise RuntimeError("servo logical zero has not been set")
        return self._count2mm(self.dev.status["apos"] - self.origin_count)

    @property
    def raw_position_mm(self) -> float:
        if not self.connected:
            raise RuntimeError("servo is not connected")
        return self._count2mm(self.dev.status["apos"])

    @property
    def setup_offset_mm(self) -> float:
        if not self.connected or self.boot_count is None:
            raise RuntimeError("servo boot reference is unavailable")
        return self._count2mm(self.dev.status["apos"] - self.boot_count)

    def set_zero(self) -> None:
        if not self.connected:
            raise RuntimeError("servo is not connected")
        if not self.dev.enable:
            raise RuntimeError("servo must be explicitly enabled before setting zero")
        self._wait_ready(self.dev)
        self.origin_count = self.dev.status["apos"]

    def jog_setup_mm(self, delta_mm: float) -> None:
        """Small boot-relative manual move before the closed position is zeroed."""
        if not self.connected:
            raise RuntimeError("servo is not connected")
        if not self.dev.enable:
            raise RuntimeError("servo must be explicitly enabled before setup jog")
        if self.origin_count is not None:
            raise RuntimeError("setup jog is only available before servo zero")
        if not math.isfinite(delta_mm) or not 0 < abs(delta_mm) <= 0.5:
            raise ValueError("manual servo jog must be nonzero and at most 0.5 mm")
        target = self.dev.status["apos"] + self._mm2count(delta_mm)
        if abs(self._count2mm(target - self.boot_count)) > self.max_expansion_mm + 1e-6:
            raise ValueError("manual servo jog exceeds boot-relative 10 mm guard")
        self._move_raw(target, self.vel_mm_s)

    def move_mm(self, mm: float, vel_mm_s: float | None = None) -> None:
        """绝对定位（变距）。"""
        if not self.connected:
            raise RuntimeError("伺服未连接")
        if not math.isfinite(mm) or not -self.max_expansion_mm <= mm <= 0:
            raise ValueError(f"servo target outside calibrated range [-{self.max_expansion_mm}, 0] mm")
        velocity = self.vel_mm_s if vel_mm_s is None else vel_mm_s
        if not math.isfinite(velocity) or velocity <= 0:
            raise ValueError("servo velocity must be finite and positive")
        if self.origin_count is None:
            raise RuntimeError("servo logical zero has not been set")
        target = self.origin_count + self._mm2count(mm)
        self._move_raw(target, velocity)

    def _move_raw(self, target: int, velocity: float) -> None:
        if not self.dev.enable:
            raise RuntimeError("servo must be enabled before motion")
        self._wait_ready(self.dev)
        with self.dev.lock:
            self.dev.vel = self._mm2count(velocity)
        self.dev.cmd_move_raw(target)
        deadline = time.monotonic() + self.timeout_s
        tolerance = max(1, self._mm2count(0.05))
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("servo move cancelled")
            status = self.dev.status
            if status["sw"] & 0x0008:
                raise RuntimeError("servo fault during move")
            if not status["wkc_ok"]:
                raise RuntimeError("EtherCAT working counter lost during move")
            if status["sw"] & 0x0400 and abs(status["apos"] - target) <= tolerance:
                return
            time.sleep(0.01)
        raise TimeoutError("servo did not reach requested position")

    def jog_mm(self, mm: float, vel_mm_s: float | None = None) -> None:
        """相对移动（变距）。"""
        self.move_mm(self.position_mm + mm, vel_mm_s)

    @property
    def fault(self) -> bool:
        return bool(self.dev and self.dev.status["sw"] & 0x0008)

    def reset_fault(self) -> None:
        if self.dev is not None:
            self.dev.reset_fault = True

    def disable(self) -> None:
        self.origin_count = None
        if self.dev is not None:
            self.dev.set_enabled(False)

    def enable(self) -> None:
        if not self.connected:
            raise RuntimeError("servo is not connected")
        self.dev.set_enabled(True)
        try:
            self._wait_ready(self.dev)
        except Exception:
            self.dev.set_enabled(False)
            raise


class Cnc3D:
    """三维模组。每次 move_to 都会等运动完成（wait_idle）。"""

    def __init__(self, port: str = "COM9", baud: int = 115200,
                 feed: int = 1500, limits=(100.0, 100.0, 50.0),
                 controller_factory=None):
        self.port = port
        self.baud = baud
        self.feed = feed
        self.limits = limits
        self.controller_factory = controller_factory
        self.cancel_event = None
        self.cnc = None

    @property
    def connected(self) -> bool:
        return self.cnc is not None

    def connect(self) -> None:
        from cnc_grbl import GrblController  # 惰性导入
        factory = self.controller_factory or GrblController
        cnc = factory(port=self.port, baudrate=self.baud)
        try:
            cnc.connect()
            rep = cnc.status()
            if (rep.state or "").lower().startswith("alarm"):
                raise RuntimeError("CNC Alarm: operator must inspect and unlock manually")
            cnc.send("G21")
            cnc.send("G90")
        except Exception:
            cnc.close()
            raise
        self.cnc = cnc

    def close(self) -> None:
        if self.cnc is not None:
            self.cnc.close()
            self.cnc = None

    def move_to(self, x: float, y: float, z: float, feed: int | None = None) -> None:
        """绝对定位并等待到位。"""
        if not self.connected:
            raise RuntimeError("三维模组未连接")
        lx, ly, lz = self.limits
        if not all(math.isfinite(v) for v in (x, y, z)) or not (0 <= x <= lx and 0 <= y <= ly and 0 <= z <= lz):
            raise ValueError(f"目标 ({x},{y},{z}) 超出软限位 (0..{lx},0..{ly},0..{lz})")
        chosen_feed = self.feed if feed is None else feed
        if not math.isfinite(chosen_feed) or chosen_feed <= 0:
            raise ValueError("CNC feed must be positive")
        self.cnc.send(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{chosen_feed}")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("CNC move cancelled")
            rep = self.cnc.status()
            state = (rep.state or "").lower()
            if state.startswith("alarm"):
                raise RuntimeError("CNC entered Alarm during move")
            if state == "idle":
                position = self.position()
                if position is not None and all(abs(actual - target) <= 0.05
                                                for actual, target in zip(position, (x, y, z))):
                    return
            time.sleep(0.1)
        raise TimeoutError("CNC did not reach requested position")

    def position(self) -> tuple[float, float, float] | None:
        """读回 WPos (x,y,z) mm；解析失败返回 None。"""
        if not self.connected:
            return None
        rep = self.cnc.status()
        def values(label: str) -> tuple[float, float, float] | None:
            m = re.search(rf"{label}:\s*([-+\d.]+)\s*,\s*([-+\d.]+)\s*,\s*([-+\d.]+)", rep.raw)
            return tuple(float(m.group(i)) for i in (1, 2, 3)) if m else None

        work = values("WPos")
        if work is not None:
            return work
        machine, offset = values("MPos"), values("WCO")
        return tuple(m - o for m, o in zip(machine, offset)) if machine and offset else None

    def home(self) -> None:
        if not self.connected:
            raise RuntimeError("三维模组未连接")
        self.cnc.home()

    def unlock(self) -> None:
        if not self.connected:
            raise RuntimeError("三维模组未连接")
        self.cnc.unlock()

    def hold(self) -> None:
        if self.cnc is not None:
            self.cnc.hold()


class InsertHead:
    """植入滑台 + 双气阀。协议（firmware/insert_head/insert_head.ino）：数字=绝对mm、z/e/d/a1/a0/b1/b0/q/s"""

    def __init__(self, port: str = "COM7", baud: int = 115200, travel_mm: float = 50.0,
                 motion=None, serial_factory=None, startup_delay_s: float = 0.8):
        self.port = port
        self.baud = baud
        self.travel = travel_mm
        self.motion = motion
        self.serial_factory = serial_factory
        self.startup_delay_s = startup_delay_s
        self._stopped = False
        self.zero_confirmed = False
        self.ser = None

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def connect(self) -> None:
        import serial  # 惰性导入
        factory = self.serial_factory or serial.Serial
        self.ser = factory(self.port, self.baud, timeout=0.1, write_timeout=1.0)
        self.zero_confirmed = False
        self._stopped = False
        try:
            time.sleep(self.startup_delay_s)  # Serial open may reset ESP32.
            self.ser.reset_input_buffer()
            self._send("v")
            self._read_until(("ZPIN_HEAD_PROTOCOL=2",), 3.0)
        except Exception as exc:
            self.close()
            raise RuntimeError("head firmware protocol 2 is required; flash and verify the current sketch") from exc

    def close(self) -> None:
        self.zero_confirmed = False
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    # ---- 底层 ----
    def _send(self, line: str) -> None:
        if not self.connected:
            raise RuntimeError("滑台未连接")
        self.ser.write((line + "\n").encode("ascii"))

    def _read_until(self, keywords: tuple[str, ...], timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        got: list[str] = []
        while time.monotonic() < deadline:
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                got.append(line)
                if "已紧急停止" in line:
                    self._stopped = True
                    raise RuntimeError("head stopped; position is untrusted")
                if line.startswith(("错误:", "ERROR:")):
                    raise RuntimeError(f"head firmware error: {line}")
                if any(k in line for k in keywords):
                    return got
        raise TimeoutError(f"滑台应答超时（等待 {keywords}），已收: {got[-3:]}")

    # ---- 业务 ----
    def position_mm(self) -> float:
        self._send("s")
        lines = self._read_until(("当前位置",), 3.0)
        m = re.search(r"当前位置:\s*([-\d.]+)", lines[-1])
        if not m:
            raise RuntimeError("head position reply is malformed")
        return float(m.group(1))

    def move_mm(self, mm: float, timeout: float = 30.0) -> float:
        """Move within the confirmed signed window after manual zero."""
        if self._stopped:
            raise RuntimeError("head stopped; operator recovery is required")
        if self.motion is None:
            raise RuntimeError("head work window has not been confirmed")
        if not math.isfinite(mm) or not self.motion.work_min_mm <= mm <= self.motion.work_max_mm:
            raise ValueError("head target outside confirmed work window")
        if not self.zero_confirmed:
            raise RuntimeError("head physical standby and software zero have not been confirmed")
        self._send(f"{mm:.3f}")
        lines = self._read_until(("到达目标", "已紧急停止"), timeout)
        echoes = [re.search(r"移动到\s*([-\d.]+)\s*mm", line) for line in lines]
        targets = [float(match.group(1)) for match in echoes if match]
        if not targets or abs(targets[-1] - mm) > 0.011:
            raise RuntimeError("head target mismatch: firmware may have clamped the command")
        return mm

    def zero(self) -> None:
        self.zero_confirmed = False
        self._send("z")
        self._read_until(("设为 0mm",), 3.0)
        self._stopped = False
        self.zero_confirmed = True

    def enable(self) -> None:
        self._send("e")
        self._read_until(("电机使能",), 3.0)

    def disable(self) -> None:
        self.zero_confirmed = False
        self._send("d")
        self._read_until(("电机释放",), 3.0)

    def valve(self, which: str, open_: bool) -> None:
        self._send(f"{which}{'1' if open_ else '0'}")
        self._read_until((f"气阀{which.upper()} 打开" if open_ else f"气阀{which.upper()} 关闭",), 3.0)

    def stop(self) -> None:
        """急停（若滑台正在运动）。"""
        self._stopped = True
        self.zero_confirmed = False
        if self.ser is not None:
            self.ser.write(b"q\n")
