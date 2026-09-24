# -*- coding: utf-8 -*-
"""多头植入设备 总控
=================================================================
控制链路：
  1. 变距模组  —— SV630N 伺服 ← EtherCAT ← PC（pysoem）
  2. 三维模组  —— ESP32(Grbl/FluidNC) ← 串口 COM9 ← PC（G代码）
  3. 植入滑台+双气阀 —— ESP32 ← 串口 ← PC（自研协议）
本程序在一个进程里协调三路控制，统一急停、统一 mm 单位。

运行：python master.py   （配置见同目录 config.toml）

命令：
  c               连接全部设备        cs/c3/ch  单独连 伺服/三维/滑台
  d3 x y z        三维模组绝对移动mm   h3/x3     三维回零($H)/解锁($X)
  a               执行单点植入流程     r         三维模组回到 (0,0,0)
  p <mm>          变距伺服绝对移动     j <mm>    变距伺服相对移动
  v <mm>          滑台绝对移动         z         滑台对零(当前位置=0)
  e / d           滑台使能/释放        a1 a0 b1 b0   气阀手动开关
  s               全部状态             !         急停(全停+关阀)
  x / quit        退出(安全关闭)
"""
from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devices import Cnc3D, InsertHead, ServoAxis

HELP = __doc__.split("命令：", 1)[-1]


class ImplantMaster:
    def __init__(self, cfg: dict):
        s, c, h, cy = cfg["servo"], cfg["cnc3d"], cfg["head"], cfg["cycle"]
        self.servo = ServoAxis(adapter=s["adapter"], lead_mm=s["lead_mm"],
                               counts_per_rev=s["counts_per_rev"],
                               vel_mm_s=s["vel_mm_s"])
        self.cnc = Cnc3D(port=c["port"], baud=c["baud"], feed=c["feed"],
                         limits=(c["x_max"], c["y_max"], c["z_max"]))
        self.head = InsertHead(port=h["port"], baud=h["baud"],
                               travel_mm=h["travel_mm"])
        self.valve_delay = cy["valve_delay_ms"] / 1000.0
        self.down_mm = cy["down_mm"]
        self.up_mm = cy["up_mm"]

    # ---------- 连接 ----------
    def connect_servo(self) -> None:
        if not self.servo.connected:
            self.servo.connect()
            print("变距伺服已连接（OP 状态，自动使能）")

    def connect_cnc(self) -> None:
        if not self.cnc.connected:
            self.cnc.connect()
            print(f"三维模组已连接 {self.cnc.port}")

    def connect_head(self) -> None:
        if not self.head.connected:
            self.head.connect()
            print(f"植入滑台已连接 {self.head.port}")

    def connect_all(self) -> None:
        errs = []
        for name, fn in (("伺服", self.connect_servo),
                         ("三维", self.connect_cnc),
                         ("滑台", self.connect_head)):
            try:
                fn()
            except Exception as ex:
                errs.append(f"{name}: {ex}")
        if errs:
            print("部分设备连接失败：\n  " + "\n  ".join(errs))
        self.check_ready()

    def check_ready(self) -> None:
        """运行自动流程前的检查清单。"""
        states = []
        for name, dev in (("伺服", self.servo), ("三维", self.cnc), ("滑台", self.head)):
            states.append(f"{name}={'已连' if dev.connected else '未连'}")
        print("设备状态: " + " | ".join(states))
        print("自动流程前提：滑台已对零(z，最上端=0)且三维模组已定位到目标孔位")

    # ---------- 单点植入流程 ----------
    def cycle_one(self) -> None:
        """开阀A → 滑台下行 → 关阀A → 开阀B → 滑台上行 → 关阀B"""
        if not self.head.connected:
            raise RuntimeError("植入滑台未连接")
        head = self.head
        pos = head.position_mm()
        down_target = max(0.0, min(head.travel, pos - self.down_mm))   # 向下=坐标减小
        up_target = max(0.0, min(head.travel, down_target + self.up_mm))

        print(f"1. 开阀A");            head.valve("a", True);  time.sleep(self.valve_delay)
        print(f"2. 滑台下行 {pos:.2f}→{down_target:.2f} mm")
        head.move_mm(down_target)
        print("3. 关阀A");            head.valve("a", False); time.sleep(self.valve_delay)
        print("4. 开阀B");            head.valve("b", True);  time.sleep(self.valve_delay)
        print(f"5. 滑台上行 {down_target:.2f}→{up_target:.2f} mm")
        head.move_mm(up_target)
        print("6. 关阀B");            head.valve("b", False); time.sleep(self.valve_delay)
        print("单点植入流程完成")

    # ---------- 急停 / 状态 / 退出 ----------
    def estop(self) -> None:
        print("!!!! 急停：滑台停+关阀, 三维hold, 伺服失能")
        try:
            self.head.stop()
            self.head.valve("a", False)
            self.head.valve("b", False)
        except Exception as ex:
            print(f"滑台急停异常: {ex}")
        try:
            self.cnc.hold()
        except Exception:
            pass
        try:
            self.servo.disable()
        except Exception:
            pass
        print("急停完成。滑台位置已不可信，恢复时请重新 z 对零")

    def status_all(self) -> None:
        if self.servo.connected:
            sv = self.servo
            print(f"[变距伺服] 位置 {sv.position_mm:+.3f} mm"
                  + ("  [故障!]" if sv.fault else ""))
        if self.cnc.connected:
            p = self.cnc.position()
            print(f"[三维模组] WPos {p}" if p else "[三维模组] 位置解析失败")
        if self.head.connected:
            print(f"[植入滑台] 位置 {self.head.position_mm():.2f} mm (0..{self.head.travel:.0f})")

    def close_all(self) -> None:
        for name, fn in (("滑台", self.head.close),
                         ("三维", self.cnc.close),
                         ("伺服", self.servo.close)):
            try:
                fn()
            except Exception:
                pass
        print("已安全退出")


def parse3(args: list[str]) -> tuple[float, float, float]:
    if len(args) < 3:
        raise ValueError("需要 3 个数: x y z")
    return tuple(float(v) for v in args[:3])  # type: ignore[return-value]


def main() -> int:
    cfg_path = Path(__file__).resolve().parent / "config.toml"
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    m = ImplantMaster(cfg)
    print("=" * 50)
    print("多头植入设备 总控  (输入 ? 查看命令)")
    print("=" * 50)

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "x"
        if not line:
            continue
        parts = line.split()
        c = parts[0].lower()
        args = parts[1:]
        try:
            if c in ("x", "quit", "exit"):
                break
            elif c == "?":
                print(HELP)
            elif c == "c":
                m.connect_all()
            elif c == "cs":
                m.connect_servo(); m.check_ready()
            elif c == "c3":
                m.connect_cnc(); m.check_ready()
            elif c == "ch":
                m.connect_head(); m.check_ready()
            elif c == "d3":
                x, y, z = parse3(args)
                m.connect_cnc() if not m.cnc.connected else None
                m.cnc.move_to(x, y, z)
                print(f"三维到位 {m.cnc.position()}")
            elif c == "r":
                m.connect_cnc() if not m.cnc.connected else None
                m.cnc.move_to(0, 0, 0)
                print("三维已回 (0,0,0)")
            elif c == "h3":
                m.connect_cnc() if not m.cnc.connected else None
                m.cnc.home()
            elif c == "x3":
                m.connect_cnc() if not m.cnc.connected else None
                m.cnc.unlock()
            elif c == "a":
                m.cycle_one()
            elif c in ("a1", "a0", "b1", "b0"):
                m.connect_head() if not m.head.connected else None
                m.head.valve(c[0], c[1] == "1")
            elif c == "p" and args:
                m.connect_servo() if not m.servo.connected else None
                m.servo.move_mm(float(args[0]))
                print(f"变距伺服位置 {m.servo.position_mm:+.3f} mm")
            elif c == "j" and args:
                m.connect_servo() if not m.servo.connected else None
                m.servo.jog_mm(float(args[0]))
                print(f"变距伺服位置 {m.servo.position_mm:+.3f} mm")
            elif c == "v" and args:
                m.connect_head() if not m.head.connected else None
                tgt = m.head.move_mm(float(args[0]))
                print(f"滑台到位 {tgt:.2f} mm")
            elif c == "z":
                m.connect_head() if not m.head.connected else None
                m.head.zero()
                print("滑台当前位置=0mm（务必是机械最上端）")
            elif c == "e":
                m.connect_head() if not m.head.connected else None
                m.head.enable()
            elif c == "d":
                m.connect_head() if not m.head.connected else None
                m.head.disable()
            elif c == "s":
                m.status_all()
            elif c == "!":
                m.estop()
            else:
                print("未知命令，? 查看帮助")
        except Exception as ex:
            print(f"错误: {ex}")
    m.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
