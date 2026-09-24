# -*- coding: utf-8 -*-
"""三个执行器的统一封装：
  ServoAxis  —— 变距模组（SV630N 伺服, EtherCAT, 复用 sv630n_pc.py 的类）
  Cnc3D      —— 三维模组 X/Y/Z（ESP32 Grbl/FluidNC, 串口, 复用 3-dim-module 库）
  InsertHead —— 植入滑台 GX28 + 双气阀（ESP32 自研协议, 见 Untitled-1.cpp）
对外全部用 mm 单位，count 换算只在 ServoAxis 内部做。
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))                      # 让 sv630n_pc 可导入
sys.path.insert(0, str(ROOT / "3-dim-module" / "src"))  # 让 cnc_grbl 可导入


class ServoAxis:
    """变距模组。mm 接口，内部换算 count（18bit 编码器 262144/圈）。"""

    def __init__(self, adapter: str = "", lead_mm: float = 1.0,
                 counts_per_rev: int = 262144, vel_mm_s: float = 1.0):
        self.adapter = adapter
        self.lead_mm = lead_mm
        self.cpr = counts_per_rev
        self.vel_mm_s = vel_mm_s
        self.dev = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def connect(self) -> None:
        from sv630n_pc import Sv630n, pick_adapter  # 惰性导入，pysoem 缺失不影响其它设备
        ifname = self.adapter or pick_adapter()
        self.dev = Sv630n(ifname)
        self.dev.open()
        self.dev.running = True
        threading.Thread(target=self.dev.cyclic, daemon=True).start()

    def close(self) -> None:
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
        return self._count2mm(self.dev.status["apos"])

    def move_mm(self, mm: float, vel_mm_s: float | None = None) -> None:
        """绝对定位（变距）。"""
        if not self.connected:
            raise RuntimeError("伺服未连接")
        with self.dev.lock:
            self.dev.vel = self._mm2count(vel_mm_s or self.vel_mm_s)
        self.dev.cmd_move(self._mm2count(mm))

    def jog_mm(self, mm: float, vel_mm_s: float | None = None) -> None:
        """相对移动（变距）。"""
        if not self.connected:
            raise RuntimeError("伺服未连接")
        with self.dev.lock:
            self.dev.vel = self._mm2count(vel_mm_s or self.vel_mm_s)
            self.dev.target = self.dev.status["apos"] + self._mm2count(mm)
            self.dev.move_req = True

    @property
    def fault(self) -> bool:
        return bool(self.dev and self.dev.status["sw"] & 0x0008)

    def reset_fault(self) -> None:
        if self.dev is not None:
            self.dev.reset_fault = True

    def disable(self) -> None:
        if self.dev is not None:
            self.dev.enable = False

    def enable(self) -> None:
        if self.dev is not None:
            self.dev.enable = True


class Cnc3D:
    """三维模组。每次 move_to 都会等运动完成（wait_idle）。"""

    def __init__(self, port: str = "COM9", baud: int = 115200,
                 feed: int = 1500, limits=(100.0, 100.0, 50.0)):
        self.port = port
        self.baud = baud
        self.feed = feed
        self.limits = limits
        self.cnc = None

    @property
    def connected(self) -> bool:
        return self.cnc is not None

    def connect(self) -> None:
        from cnc_grbl import GrblController  # 惰性导入
        self.cnc = GrblController(port=self.port, baudrate=self.baud)
        self.cnc.connect()
        rep = self.cnc.status()
        if (rep.state or "").lower().startswith("alarm"):
            print("三维模组处于 Alarm，尝试 $X 解锁")
            self.cnc.unlock()

    def close(self) -> None:
        if self.cnc is not None:
            self.cnc.close()
            self.cnc = None

    def move_to(self, x: float, y: float, z: float, feed: int | None = None) -> None:
        """绝对定位并等待到位。"""
        if not self.connected:
            raise RuntimeError("三维模组未连接")
        lx, ly, lz = self.limits
        if not (0 <= x <= lx and 0 <= y <= ly and 0 <= z <= lz):
            raise ValueError(f"目标 ({x},{y},{z}) 超出软限位 (0..{lx},0..{ly},0..{lz})")
        self.cnc.send(f"G90 G0 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feed or self.feed}")
        self.cnc.wait_idle()

    def position(self) -> tuple[float, float, float] | None:
        """读回 WPos (x,y,z) mm；解析失败返回 None。"""
        if not self.connected:
            return None
        rep = self.cnc.status()
        m = re.search(r"WPos:\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)", rep.raw)
        return (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else None

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
    """植入滑台 + 双气阀。协议（Untitled-1.cpp）：数字=绝对mm、z/e/d/a1/a0/b1/b0/q/s"""

    def __init__(self, port: str = "COM7", baud: int = 115200, travel_mm: float = 50.0):
        self.port = port
        self.baud = baud
        self.travel = travel_mm
        self.ser = None

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def connect(self) -> None:
        import serial  # 惰性导入
        self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        time.sleep(0.8)              # ESP32 若被 DTR 复位，等它打出帮助信息
        self.ser.reset_input_buffer()

    def close(self) -> None:
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
        self.ser.reset_input_buffer()
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
                if any(k in line for k in keywords):
                    return got
        raise TimeoutError(f"滑台应答超时（等待 {keywords}），已收: {got[-3:]}")

    # ---- 业务 ----
    def position_mm(self) -> float:
        self._send("s")
        lines = self._read_until(("当前位置",), 3.0)
        m = re.search(r"当前位置:\s*([-\d.]+)", lines[-1])
        return float(m.group(1)) if m else float("nan")

    def move_mm(self, mm: float, timeout: float = 30.0) -> float:
        """绝对移动并等待到位，返回实际目标值（软限位 0..travel）。"""
        mm = max(0.0, min(self.travel, mm))
        self._send(f"{mm:.3f}")
        lines = self._read_until(("到达目标", "已紧急停止"), timeout)
        for ln in lines:
            if "已紧急停止" in ln:
                raise RuntimeError("滑台被急停，位置已不可信，需重新对零")
        return mm

    def zero(self) -> None:
        self._send("z")
        self._read_until(("设为 0mm",), 3.0)

    def enable(self) -> None:
        self._send("e")
        self._read_until(("电机使能",), 3.0)

    def disable(self) -> None:
        self._send("d")
        self._read_until(("电机释放",), 3.0)

    def valve(self, which: str, open_: bool) -> None:
        self._send(f"{which}{'1' if open_ else '0'}")
        self._read_until((f"气阀{which.upper()} 打开" if open_ else f"气阀{which.upper()} 关闭",), 3.0)

    def stop(self) -> None:
        """急停（若滑台正在运动）。"""
        if self.ser is not None:
            self.ser.write(b"q\n")
