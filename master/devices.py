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
from dataclasses import dataclass
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
                 device_factory=None, echo=None):
        self.adapter = adapter
        self.lead_mm = lead_mm
        self.cpr = counts_per_rev
        self.vel_mm_s = vel_mm_s
        self.max_expansion_mm = max_expansion_mm
        self.timeout_s = timeout_s
        self.device_factory = device_factory
        self.echo = echo
        self.cancel_event = None
        self.origin_count = None
        self.boot_count = None
        self.dev = None

    # 驱动器没有行协议，"回话"就是状态字。echo=None 时全部静默。
    def _say_tx(self, text: str) -> None:
        if self.echo is not None:
            self.echo.tx("伺服", text)

    def _say_rx(self, text: str) -> None:
        if self.echo is not None:
            self.echo.rx("伺服", text)

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def connect(self) -> None:
        if self.connected:
            return
        if not self.adapter:
            raise ValueError("必须先明确选择 EtherCAT 网卡")
        if self.device_factory is None:
            from sv630n_pc import LEAD_MM, Sv630n
            if not math.isclose(self.lead_mm, LEAD_MM, rel_tol=1e-6):
                raise ValueError("伺服 lead_mm 与标定的 LEAD_MM 不一致")
            factory = Sv630n
            # 配置里存的是人类可读的网卡描述；只有真实硬件路径才解析成 pysoem 设备名。
            # 注入 device_factory 的测试用的是 "fake-wired" 这类假值，绝不能去查真实网卡。
            adapter = resolve_adapter(self.adapter)
        else:
            factory = self.device_factory
            adapter = self.adapter
        dev = factory(adapter)
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
        status = dev.status
        self._say_rx(f"链路就绪 状态字=0x{status['sw']:04X} "
                     f"位置={self._count2mm(status['apos']):+.3f} mm")

    def _wait_link(self, dev) -> None:
        """等链路进入稳态。驱动器刚进 OP 时会瞬时置故障位（本机实测约 5 ms，故障码 0x603F = 0）
        然后自行清除；每 10 ms 轮询一次去抓那 5 ms 是纯竞态，所以这里等的是"健康稳态"，
        只有故障位一直不清才判死。动之前的 _wait_ready 仍然严格，不动。"""
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            status = dev.status
            if status["wkc_ok"] and not status["sw"] & 0x0008:
                return
            time.sleep(0.01)
        if not dev.status["wkc_ok"]:
            self._say_rx("链路故障：工作计数器始终无效")
            raise TimeoutError("EtherCAT 工作计数器始终无效")
        self._say_rx(f"链路故障 状态字=0x{dev.status['sw']:04X}")
        raise RuntimeError(f"伺服连接时故障：状态字=0x{dev.status['sw']:04X}")

    def _wait_ready(self, dev) -> None:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("伺服等待被取消")
            status = dev.status
            if status["sw"] & 0x0008:
                self._say_rx(f"故障 状态字=0x{status['sw']:04X}")
                raise RuntimeError(f"伺服动作前故障：状态字=0x{status['sw']:04X}")
            if status["wkc_ok"] and status["sw"] & 0x006F == 0x0027:
                return
            time.sleep(0.01)
        if not dev.status["wkc_ok"]:
            raise TimeoutError("EtherCAT 工作计数器始终无效")
        raise TimeoutError("伺服未进入使能就绪状态")

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
            raise RuntimeError("伺服逻辑零位尚未设置")
        return self._count2mm(self.dev.status["apos"] - self.origin_count)

    @property
    def raw_position_mm(self) -> float:
        if not self.connected:
            raise RuntimeError("伺服未连接")
        return self._count2mm(self.dev.status["apos"])

    @property
    def setup_offset_mm(self) -> float:
        if not self.connected or self.boot_count is None:
            raise RuntimeError("伺服上电基准不可用")
        return self._count2mm(self.dev.status["apos"] - self.boot_count)

    def set_zero(self) -> None:
        if not self.connected:
            raise RuntimeError("伺服未连接")
        if not self.dev.enable:
            raise RuntimeError("设置零位前必须先使能伺服")
        self._say_tx("记录当前点为本次连接的伺服逻辑零位")
        self._wait_ready(self.dev)
        self.origin_count = self.dev.status["apos"]

    def jog_setup_mm(self, delta_mm: float) -> None:
        """Small boot-relative manual move before the closed position is zeroed."""
        if not self.connected:
            raise RuntimeError("伺服未连接")
        if not self.dev.enable:
            raise RuntimeError("点动前必须先使能伺服")
        if self.origin_count is not None:
            raise RuntimeError("点动只能在伺服设零之前进行")
        if not math.isfinite(delta_mm) or not 0 < abs(delta_mm) <= 0.5:
            raise ValueError("手动点动距离必须非零且不超过 0.5 mm")
        target = self.dev.status["apos"] + self._mm2count(delta_mm)
        if abs(self._count2mm(target - self.boot_count)) > self.max_expansion_mm + 1e-6:
            raise ValueError("手动点动超出相对上电基准 10 mm 的保护范围")
        self._say_tx(f"点动 {delta_mm:+.3f} mm")
        self._move_raw(target, self.vel_mm_s)

    def move_mm(self, mm: float, vel_mm_s: float | None = None) -> None:
        """绝对定位（变距）。"""
        if not self.connected:
            raise RuntimeError("伺服未连接")
        if not math.isfinite(mm) or not -self.max_expansion_mm <= mm <= 0:
            raise ValueError(f"伺服目标超出标定范围 [-{self.max_expansion_mm}, 0] mm")
        velocity = self.vel_mm_s if vel_mm_s is None else vel_mm_s
        if not math.isfinite(velocity) or velocity <= 0:
            raise ValueError("伺服速度必须是有限正数")
        if self.origin_count is None:
            raise RuntimeError("伺服逻辑零位尚未设置")
        target = self.origin_count + self._mm2count(mm)
        self._say_tx(f"绝对定位 {mm:+.3f} mm")
        self._move_raw(target, velocity)

    def _move_raw(self, target: int, velocity: float) -> None:
        if not self.dev.enable:
            raise RuntimeError("动作前必须使能伺服")
        self._wait_ready(self.dev)
        with self.dev.lock:
            self.dev.vel = self._mm2count(velocity)
        self.dev.cmd_move_raw(target)
        deadline = time.monotonic() + self.timeout_s
        tolerance = max(1, self._mm2count(0.05))
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("伺服动作被取消")
            status = self.dev.status
            if status["sw"] & 0x0008:
                self._say_rx(f"故障 状态字=0x{status['sw']:04X}")
                raise RuntimeError(f"伺服运动中故障：状态字=0x{status['sw']:04X}")
            if not status["wkc_ok"]:
                self._say_rx("工作计数器在运动中丢失")
                raise RuntimeError("EtherCAT 工作计数器在运动中丢失")
            if status["sw"] & 0x0400 and abs(status["apos"] - target) <= tolerance:
                self._say_rx(f"状态字=0x{status['sw']:04X} 到位")
                return
            time.sleep(0.01)
        raise TimeoutError("伺服未到达目标位置")

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
            self._say_tx("释放（清空逻辑零位）")
            self.dev.set_enabled(False)

    def enable(self) -> None:
        if not self.connected:
            raise RuntimeError("伺服未连接")
        self._say_tx("使能")
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
                 controller_factory=None, echo=None):
        self.port = port
        self.baud = baud
        self.feed = feed
        self.limits = limits
        self.controller_factory = controller_factory
        self.echo = echo
        self.cancel_event = None
        self.cnc = None

    @property
    def connected(self) -> bool:
        return self.cnc is not None

    def connect(self) -> None:
        from cnc_grbl import GrblController  # 惰性导入
        factory = self.controller_factory or GrblController
        options = {}
        if self.echo is not None:
            from master.echo import GrblTrace  # 惰性导入，--smoke-test 不加载回显层
            options["trace"] = GrblTrace(self.echo, "CNC")
        cnc = factory(port=self.port, baudrate=self.baud, **options)
        try:
            cnc.connect()
            rep = cnc.status()
            if (rep.state or "").lower().startswith("alarm"):
                raise RuntimeError(
                    f"CNC 进入 Alarm 报警：需人工检查并解锁（{rep.raw}）")
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
        # Z 只查上界。这三个是 work-coordinate limit，工件坐标本来就可负：对刀点是针尖
        # 接触管顶母线的高度，排间安全高度与排内工作 Z 在同一个坐标系里读回来，自然也
        # 是负的。硬加下界会逼操作员重设一个向导从没要求过的 Z 工件零点。X/Y 的下界
        # 保留——排头就是 X=0，往负走是回到工件外面。防扎管靠的是排间安全高度那条检查。
        if not all(math.isfinite(v) for v in (x, y, z)) or not (0 <= x <= lx and 0 <= y <= ly and z <= lz):
            raise ValueError(f"目标 ({x},{y},{z}) 超出软限位 (X 0..{lx}, Y 0..{ly}, Z 上限 {lz})")
        chosen_feed = self.feed if feed is None else feed
        if not math.isfinite(chosen_feed) or chosen_feed <= 0:
            raise ValueError("CNC 进给速度必须为正")
        self.cnc.send(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{chosen_feed}")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise RuntimeError("CNC 动作被取消")
            rep = self.cnc.status()
            state = (rep.state or "").lower()
            if state.startswith("alarm"):
                raise RuntimeError(f"CNC 动作中进入 Alarm 报警：{rep.raw}")
            if state == "idle":
                position = self.position()
                if position is not None and all(abs(actual - target) <= 0.05
                                                for actual, target in zip(position, (x, y, z))):
                    return
            time.sleep(0.1)
        raise TimeoutError("CNC 未到达目标位置")

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
                 motion=None, serial_factory=None, startup_delay_s: float = 0.8,
                 echo=None):
        self.port = port
        self.baud = baud
        self.travel = travel_mm
        self.motion = motion
        self.serial_factory = serial_factory
        self.startup_delay_s = startup_delay_s
        self.echo = echo
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
            raise RuntimeError(f"GX28 滑台 {self.port} 协议 2 握手失败：{exc}") from exc

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
        if self.echo is not None:
            self.echo.tx("GX28", line)
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
                if self.echo is not None:
                    # 固件原话先落地，主控的转述随后——出错时也得看得见设备说了什么。
                    self.echo.rx("GX28", line)
                if "ZPIN_HEAD_PROTOCOL=2" in keywords:
                    if "invalid header:" in line.lower():
                        raise RuntimeError(
                            f"ESP32 启动异常（{line}），固件尚未运行；"
                            "与已知 GPIO12/Flash 供电问题现象一致。"
                            "请将 USB 与 Shield/驱动电源完全断开后冷启动，勿按 RESET；"
                            "该回话不能证明固件需要重烧")
                    if line.lower().startswith(("grbl", "error:", "alarm:")):
                        raise RuntimeError(
                            f"收到 CNC/GRBL 风格回话 {line!r}；"
                            "请核对滑台串口是否误选为 CNC 串口")
                    if line.startswith("ZPIN_HEAD_PROTOCOL=") and line != "ZPIN_HEAD_PROTOCOL=2":
                        raise RuntimeError(f"设备报告 {line!r}，与要求的协议 2 不匹配，请核对固件版本")
                if "已紧急停止" in line:
                    self._stopped = True
                    raise RuntimeError(f"滑台已急停，位置不可信：{line}")
                if line.startswith(("错误:", "ERROR:")):
                    raise RuntimeError(f"滑台固件报错：{line}")
                if any(k in line for k in keywords):
                    return got
        raise TimeoutError(f"滑台应答超时（等待 {keywords}），已收: {got[-3:]}")

    # ---- 业务 ----
    def position_mm(self) -> float:
        self._send("s")
        lines = self._read_until(("当前位置",), 3.0)
        m = re.search(r"当前位置:\s*([-\d.]+)", lines[-1])
        if not m:
            raise RuntimeError("滑台位置回话格式不对")
        return float(m.group(1))

    def move_mm(self, mm: float, timeout: float = 30.0) -> float:
        """Move within the confirmed signed window after manual zero."""
        if self._stopped:
            raise RuntimeError("滑台已急停，需操作员手动恢复")
        if self.motion is None:
            raise RuntimeError("滑台工作窗口尚未确认")
        if not math.isfinite(mm) or not self.motion.work_min_mm <= mm <= self.motion.work_max_mm:
            raise ValueError("滑台目标超出已确认的工作窗口")
        if not self.zero_confirmed:
            raise RuntimeError("滑台物理待机位与软件零位尚未确认")
        self._send(f"{mm:.3f}")
        lines = self._read_until(("到达目标", "已紧急停止"), timeout)
        echoes = [re.search(r"移动到\s*([-\d.]+)\s*mm", line) for line in lines]
        targets = [float(match.group(1)) for match in echoes if match]
        if not targets or abs(targets[-1] - mm) > 0.011:
            raise RuntimeError("滑台目标不一致：固件可能截断了下发值")
        readback = self.position_mm()
        if abs(readback - mm) > 0.05:
            raise RuntimeError(f"滑台位置回读 {readback:g} mm 与目标 {mm:g} mm 不符")
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

    def _read_stop_ack(self, timeout: float = 1.0) -> None:
        """读到 `q` 的回执为止。只回显，不判定，不抛。

        固件的 emergencyStop() 只打一行「已紧急停止！位置已不准确，请重新 z 回零」，
        所以读到这一行就够。**不能把缓冲区读空**——那等于替下一条命令吃掉它的应答。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line and self.echo is not None:
                self.echo.rx("GX28", line)
            if "已紧急停止" in line:
                return

    def stop(self) -> None:
        """急停（若滑台正在运动）。

        写完 `q` 必须把回执读掉再返回。固件回的那行留在缓冲区里，会被**下一条**命令
        的 `_read_until` 当成自己的回执读到——于是「关闭 a 阀」被记成失败，故障报告
        变成「安全状态未确认」，而固件的 emergencyStop() 其实已经把两个阀都关了。
        是程序没法确认安全，不是机器不安全。

        急停路径本身绝不能再抛异常：读不到就到此为止，`_stopped` 已经置起了。
        """
        self._stopped = True
        self.zero_confirmed = False
        if self.ser is None:
            return
        try:
            self.ser.write(b"q\n")
            self._read_stop_ack()
        except Exception:
            pass


# ---------------------------------------------------------------- 枚举与解析
# 向导用。放在本模块是因为这里已经是 pysoem 和 pyserial 的唯一惰性导入边界，
# 而 resolve_adapter 必须能从 ServoAxis.connect() 直接调用。
# 纯函数都接受注入的枚举器，所以无硬件也能测。

@dataclass(frozen=True)
class SerialCandidate:
    device: str
    description: str
    hwid: str
    is_usb: bool
    reject_reason: str | None


@dataclass(frozen=True)
class NicCandidate:
    index: int
    description: str
    name: str
    reject_reason: str | None


# 顺序有意义：先匹配更具体的原因，"Microsoft Wi-Fi Direct Virtual Adapter"
# 同时命中 wi-fi direct 和 virtual，操作员该看到的是前者。
_NIC_REJECTIONS: tuple[tuple[str, str], ...] = (
    ("wan miniport", "Windows 拨号虚拟网卡"),
    ("wi-fi direct", "Wi-Fi Direct 虚拟网卡"),
    ("loopback", "回环抓包适配器"),
    ("bluetooth", "蓝牙个人网卡"),
    ("zerotier", "ZeroTier 虚拟网卡"),
    ("radmin", "Radmin VPN 虚拟网卡"),
    ("tap-win32", "TAP 虚拟网卡"),
    ("tap-windows", "TAP 虚拟网卡"),
    ("netease", "网易 UU 虚拟网卡"),
    ("hyper-v", "Hyper-V 虚拟交换机"),
    ("vmware", "VMware 虚拟网卡"),
    ("virtualbox", "VirtualBox 虚拟网卡"),
    ("virtual", "虚拟网卡"),
    ("802.11", "无线网卡，不能用于 EtherCAT"),
    ("wi-fi", "无线网卡，不能用于 EtherCAT"),
    ("wifi", "无线网卡，不能用于 EtherCAT"),
    ("wireless", "无线网卡，不能用于 EtherCAT"),
    ("wlan", "无线网卡，不能用于 EtherCAT"),
)


def list_serial_ports(list_ports=None) -> list[SerialCandidate]:
    """枚举串口。惰性导入 pyserial，smoke/simulate 不会碰到串口机制。"""
    if list_ports is None:
        from serial.tools import list_ports as module
        list_ports = module.comports
    return [_serial_candidate(port) for port in list_ports()]


def _serial_candidate(port) -> SerialCandidate:
    hwid = getattr(port, "hwid", "") or ""
    upper = hwid.upper()
    is_usb = upper.startswith("USB") or "VID:PID" in upper
    return SerialCandidate(
        device=getattr(port, "device", "") or "",
        description=getattr(port, "description", "") or "",
        hwid=hwid,
        is_usb=is_usb,
        reject_reason=None if is_usb else "不是 USB 串口（蓝牙或虚拟端口）",
    )


def rank_serial_ports(ports) -> list[SerialCandidate]:
    """USB 串口在前，同组内按 COM 号排序。返回全表，不过滤。"""
    return sorted(ports, key=lambda port: (not port.is_usb, _com_index(port.device),
                                           port.device))


def _com_index(device: str) -> int:
    match = re.fullmatch(r"COM(\d+)", device.upper())
    return int(match.group(1)) if match else 10 ** 6


def list_nics(find_adapters=None) -> list[NicCandidate]:
    """枚举抓包适配器。失败一律转成带 Npcap 提示的 RuntimeError。"""
    try:
        if find_adapters is None:
            import pysoem
            find_adapters = pysoem.find_adapters
        adapters = list(find_adapters())
    except Exception as exc:
        raise RuntimeError(f"枚举网卡失败：{exc}；请确认已安装 Npcap 和 pysoem") from exc
    candidates = []
    for index, adapter in enumerate(adapters):
        description = _decode_desc(getattr(adapter, "desc", ""))
        candidates.append(NicCandidate(index, description, str(getattr(adapter, "name", "")),
                                       nic_reject_reason(description)))
    return candidates


def _decode_desc(raw) -> str:
    """pysoem 的 desc 是 bytes（本机实测），可能含 ® 之类非 ASCII。"""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def nic_reject_reason(description: str) -> str | None:
    """不适合 EtherCAT 的网卡给出中文原因；返回 None 表示是候选。"""
    lowered = description.lower()
    for pattern, reason in _NIC_REJECTIONS:
        if pattern in lowered:
            return reason
    return None


def rank_nics(nics) -> list[NicCandidate]:
    """候选网卡在前，其余按枚举顺序跟随。返回全表，不过滤。"""
    return sorted(nics, key=lambda nic: (nic.reject_reason is not None, nic.index))


def resolve_adapter(description: str, *, find_adapters=None) -> str:
    """人类可读网卡描述 → 当前 pysoem 设备名。

    已经是 ``\\Device\\NPF_...`` 的值原样返回，ENVIRONMENT.md 里的手工流程继续可用。
    这里只做解析、不做过滤：菜单的过滤只是提示，USB 网卡转 EtherCAT 会被误过滤，
    所以被过滤的描述仍然必须能解析出来。
    """
    if not description:
        raise ValueError("EtherCAT adapter must be selected explicitly")
    if description.startswith("\\Device\\NPF_"):
        return description
    nics = list_nics(find_adapters)
    matches = [nic for nic in nics if nic.description == description]
    if not matches:
        folded = description.strip().lower()
        matches = [nic for nic in nics if nic.description.strip().lower() == folded]
    if not matches:
        matches = [nic for nic in nics if description.strip().lower() in nic.description.lower()]
    if not matches:
        raise ValueError(f"找不到网卡“{description}”；网卡可能已更换或改名，请重新运行向导")
    if len({nic.name for nic in matches}) > 1:
        listing = "、".join(sorted(nic.description for nic in matches))
        raise ValueError(f"网卡描述“{description}”不唯一（{listing}）；"
                         f"请在 config.local.toml 的 [servo] adapter 里改填 pysoem 设备名")
    return matches[0].name
