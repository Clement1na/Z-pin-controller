# -*- coding: utf-8 -*-
"""
PC 有线网口直接作为 EtherCAT 主站控制 SV630N 伺服（CiA402 PP 轮廓定位模式）
=================================================================
依赖：Python 3.12 + Npcap(WinPcap 兼容模式) + pysoem
接线：电脑网口 ——网线——> SV630N 的 CN3(IN) 网口，中间不接交换机
运行：python sv630n_pc.py [网卡序号]

本脚本自动完成（无需在驱动器面板做任何设置）：
  1. 把 PDO 映射改为 PP 模式推荐配置（手册 11.7.2）：
     RPDO: 6040h 控制字 + 607Ah 目标位置 + 6081h 运行速度
     TPDO: 6041h 状态字 + 6064h 实际位置
  2. 写 6060h=1 切换到轮廓位置模式（手册：模式由 6060h 设置）
  3. CiA402 使能状态机（0x06→0x07→0x0F）
  4. 发目标位置 + 控制字 bit4 上升沿触发定位

交互命令（单位：mm / mm/s，脚本自动换算成 count）：
  e        伺服使能          d        伺服失能
  r        故障复位          m <mm>   绝对定位到 mm
  j <mm>   相对移动 mm       v <mm/s> 设置速度
  p        打印 PDO 映射     s        刷新状态显示
  q        退出（自动失能）

位置换算（18bit 编码器，262144 count/电机圈）：
  count = mm / LEAD_MM * 262144     （LEAD_MM = 电机转一圈滑台移动的毫米数）
"""
import queue
import struct
import sys
import threading
import time

import pysoem

CYCLE_S = 0.002  # PDO 周期 2ms，PP 模式足够

# ===== 距离换算 =====
LEAD_MM = 13.3333333           # ★ 等效导程：电机每转一圈，变距模组间距变化多少 mm（实测标定：0.812圈≈12mm）
COUNTS_PER_REV = 262144   # 18bit 编码器，固定不变


def mm2count(mm: float) -> int:
    return int(round(mm / LEAD_MM * COUNTS_PER_REV))


def count2mm(c: float) -> float:
    return c / COUNTS_PER_REV * LEAD_MM


# ===== 软件限位（变距模组间距允许范围）=====
SOFT_MAX_MM = 0.0      # 间距上限
SOFT_MIN_MM = -10.0    # 间距下限
SOFT_MAX_CNT = mm2count(SOFT_MAX_MM)
SOFT_MIN_CNT = mm2count(SOFT_MIN_MM)


def clamp_count(c: int) -> tuple[int, bool]:
    """把目标位置(count)钳位到软限位 [SOFT_MIN_CNT, SOFT_MAX_CNT] 内，返回(钳位后值, 是否被钳)"""
    lo = min(SOFT_MIN_CNT, SOFT_MAX_CNT)
    hi = max(SOFT_MIN_CNT, SOFT_MAX_CNT)
    if c < lo:
        return lo, True
    if c > hi:
        return hi, True
    return c, False

# PP 模式推荐 PDO 映射（对象索引, 子索引, 位数）——手册 11.7.2
RPDO_MAP = [(0x6040, 0x00, 16),   # 控制字
            (0x607A, 0x00, 32),   # 目标位置
            (0x6081, 0x00, 32)]   # 轮廓运行速度
TPDO_MAP = [(0x6041, 0x00, 16),   # 状态字
            (0x6064, 0x00, 32)]   # 实际位置


def u16(x):
    return struct.pack('<H', x)


def u32(x):
    return struct.pack('<I', x)


def remap_pp_pdo(slave):
    """按手册 14.3 流程把 0x1600/0x1A00 配成 PP 映射（必须在 PreOP 阶段执行）"""
    for assign, mapidx, entries in ((0x1C12, 0x1600, RPDO_MAP),
                                    (0x1C13, 0x1A00, TPDO_MAP)):
        try:
            slave.sdo_write(assign, 0, u16(0))                    # 清除 PDO 分配组
            slave.sdo_write(mapidx, 0, struct.pack('<B', 0))      # 清除映射对象
            for i, (idx, sub, bits) in enumerate(entries, start=1):
                slave.sdo_write(mapidx, i, u32((idx << 16) | (sub << 8) | bits))
            slave.sdo_write(mapidx, 0, struct.pack('<B', len(entries)))
            slave.sdo_write(assign, 1, u16(mapidx))               # 写入分配组
            slave.sdo_write(assign, 0, u16(1))
        except Exception as ex:
            raise RuntimeError(f'SDO 0x{assign:04X}/0x{mapidx:04X} 失败: {ex!r}') from ex
    print('PDO 已重映射为 PP 模式配置: RPDO[6040+607A+6081] TPDO[6041+6064]')


def _count(raw: bytes) -> int:
    """0x1C12/0x1A00 子索引0 的'条目数'：有的设备回1字节(U8)，有的回2字节(U16)"""
    if len(raw) == 1:
        return raw[0]
    return struct.unpack('<H', raw[:2])[0]


def find_pdo_bit_offset(slave, assign_idx, obj_idx, obj_sub):
    """遍历 PDO 分配表，返回对象在过程映像中的字节偏移，找不到返回 -1"""
    bit_off = 0
    cnt = _count(slave.sdo_read(assign_idx, 0))
    for i in range(1, cnt + 1):
        pdo = struct.unpack('<H', slave.sdo_read(assign_idx, i))[0]
        n = slave.sdo_read(pdo, 0)[0]
        for j in range(1, n + 1):
            ent = struct.unpack('<I', slave.sdo_read(pdo, j))[0]
            e_idx, e_sub, bits = (ent >> 16) & 0xFFFF, (ent >> 8) & 0xFF, ent & 0xFF
            if e_idx == obj_idx and e_sub == obj_sub:
                return bit_off // 8
            bit_off += bits
    return -1


def print_pdo_map(slave):
    for name, assign in (('RPDO(主站->驱动)', 0x1C12), ('TPDO(驱动->主站)', 0x1C13)):
        print(f'--- {name} ---')
        cnt = _count(slave.sdo_read(assign, 0))
        for i in range(1, cnt + 1):
            pdo = struct.unpack('<H', slave.sdo_read(assign, i))[0]
            n = slave.sdo_read(pdo, 0)[0]
            for j in range(1, n + 1):
                ent = struct.unpack('<I', slave.sdo_read(pdo, j))[0]
                e_idx, e_sub, bits = (ent >> 16) & 0xFFFF, (ent >> 8) & 0xFF, ent & 0xFF
                print(f'  0x{e_idx:04X}:{e_sub:02X}  {bits}bit')


class Sv630n:
    def __init__(self, ifname):
        self.master = pysoem.Master()
        self.slave = None
        self.ifname = ifname
        self.off_cw = self.off_tp = self.off_pv = -1
        self.off_sw = self.off_ap = -1
        self.running = False
        self.lock = threading.Lock()
        self.enable = False            # 默认失能，输入 e 才使能（禁止上电自动锁轴）
        self.reset_fault = False
        self.move_req = False
        self.trig_pending = False
        self.target = 0
        self.vel = 262144             # 默认 1 圈/s（= LEAD_MM mm/s，1mm/s 慢速安全）
        self.sdo_jobs = []
        self.status = dict(sw=0, apos=0, wkc_ok=False)
        self._fault_latched = False

    def open(self):
        self.master.open(self.ifname)
        n = self.master.config_init()
        if n <= 0:
            raise RuntimeError('没找到 EtherCAT 从站：检查网线是否接驱动器 CN3(IN) 口、驱动器是否上电')
        self.slave = self.master.slaves[0]
        print(f'找到从站: {self.slave.name}  厂商ID:0x{self.slave.man:08X}  产品码:0x{self.slave.id:08X}')

        # --- PreOP 阶段：重映射 PDO + 设置 PP 模式 ---
        try:
            remap_pp_pdo(self.slave)
        except Exception as ex:
            print(f'PDO 重映射失败({ex})，尝试直接使用当前映射')
        # 6060 是 INT8(1字节)，6083/6084 是 U32(4字节)，写错长度会被 SDO abort 0x06070013
        for idx, val, desc, fmt in ((0x6060, 1, '运行模式=PP(轮廓位置)', '<b'),
                                    (0x6083, 500000, '加速度', '<I'),
                                    (0x6084, 500000, '减速度', '<I')):
            try:
                self.slave.sdo_write(idx, 0, struct.pack(fmt, val))
                print(f'SDO 0x{idx:04X} {desc} OK')
            except Exception as ex:
                print(f'SDO 0x{idx:04X} {desc} 失败(可忽略): {ex!r}')

        self.master.config_map()
        self.master.config_dc()
        self.master.state_check(pysoem.SAFEOP_STATE, 50000)

        # 确认对象偏移
        self.off_cw = find_pdo_bit_offset(self.slave, 0x1C12, 0x6040, 0)
        self.off_tp = find_pdo_bit_offset(self.slave, 0x1C12, 0x607A, 0)
        self.off_pv = find_pdo_bit_offset(self.slave, 0x1C12, 0x6081, 0)
        self.off_sw = find_pdo_bit_offset(self.slave, 0x1C13, 0x6041, 0)
        self.off_ap = find_pdo_bit_offset(self.slave, 0x1C13, 0x6064, 0)
        if min(self.off_cw, self.off_tp, self.off_sw, self.off_ap) < 0:
            raise RuntimeError('PDO 中仍缺少 6040/607A/6041/6064，请把 p 命令输出发给助手分析')

        # 输出清零后请求 OP
        self.slave.output = bytes(len(self.slave.output))
        self.master.state = pysoem.OP_STATE
        self.master.send_processdata()
        self.master.receive_processdata(2000)
        self.master.write_state()
        for _ in range(20):
            if self.master.state_check(pysoem.OP_STATE, 100000) == pysoem.OP_STATE:
                break
            self.master.send_processdata()
            self.master.receive_processdata(2000)
        if self.master.state != pysoem.OP_STATE:
            raise RuntimeError(f'无法进入 OP 状态，AL状态码: 0x{self.slave.al_status:04X}')
        print('EtherCAT OP 状态达成，2ms 周期数据交换已启动\n')

    # ---------------- 周期线程 ----------------
    def cyclic(self):
        while self.running:
            self.master.send_processdata()
            wkc = self.master.receive_processdata(2000)
            inp = self.slave.input
            sw = struct.unpack_from('<H', inp, self.off_sw)[0]
            apos = struct.unpack_from('<i', inp, self.off_ap)[0]

            with self.lock:
                enable, reset, move, target, vel = (self.enable, self.reset_fault,
                                                    self.move_req, self.target, self.vel)
                if move and enable and (sw & 0x006F) == 0x0027:
                    self.move_req = False
                self.reset_fault = False

            if (sw & 0x0008) and not self._fault_latched:
                self._fault_latched = True
                with self.lock:
                    self.sdo_jobs.append(('rd', 0x603F, None))
            elif not (sw & 0x0008):
                self._fault_latched = False

            out = bytearray(self.slave.output)
            cw = 0x0006
            if sw & 0x0008:                       # 故障
                cw = 0x0080 if reset else 0x0000
            else:
                st = sw & 0x006F
                if st == 0x40:                    # Switch-on disabled
                    cw = 0x0006 if enable else 0x0000
                elif st == 0x21:                  # Ready to switch on
                    cw = 0x0007 if enable else 0x0006
                elif st == 0x23:                  # Switched on
                    cw = 0x000F if enable else 0x0006
                elif st == 0x27:                  # Operation enabled（CiA402: sw&0x6F==0x27）
                    if move and enable:
                        struct.pack_into('<i', out, self.off_tp, target)
                        cw = 0x001F               # 0x0F + bit4 New Setpoint（bit6=0 绝对定位）
                        self.trig_pending = True
                    elif self.trig_pending:
                        cw = 0x000F               # 撤销触发位（下降沿）
                        self.trig_pending = False
                    else:
                        cw = 0x000F
            if self.off_pv >= 0:                  # 6081 在 PDO 里，速度走 PDO
                struct.pack_into('<i', out, self.off_pv, vel)
            struct.pack_into('<H', out, self.off_cw, cw)
            self.slave.output = bytes(out)

            with self.lock:
                jobs, self.sdo_jobs = self.sdo_jobs, []
            for job in jobs:
                try:
                    op = job[0]
                    if op == 'wr':                          # 32bit 有符号写 sub0
                        _, idx, val = job
                        self.slave.sdo_write(idx, 0, struct.pack('<i', val))
                        print(f'\nSDO 写 0x{idx:04X}:00 = {val} OK')
                    elif op == 'set':                       # 16bit 无符号写指定 sub
                        _, idx, sub, val = job
                        self.slave.sdo_write(idx, sub, struct.pack('<H', val))
                        print(f'\nSDO 写 0x{idx:04X}:{sub:02X} = {val} OK')
                    elif op == 'get':                       # 读指定 sub（兼容 1/2 字节）
                        _, idx, sub = job
                        raw = self.slave.sdo_read(idx, sub)
                        v = raw[0] if len(raw) == 1 else struct.unpack('<H', raw[:2])[0]
                        print(f'\nSDO 读 0x{idx:04X}:{sub:02X} = {v}')
                    else:                                   # 'rd' 读故障码
                        _, idx, _ = job
                        (code,) = struct.unpack('<H', self.slave.sdo_read(idx, 0))
                        print(f'\n驱动器故障码 0x603F = 0x{code:04X}（查手册故障表）')
                except Exception as ex:
                    print(f'\nSDO 失败: {ex}')

            with self.lock:
                self.status = dict(sw=sw, apos=apos, wkc_ok=wkc >= self.master.expected_wkc)
            time.sleep(CYCLE_S)

    # ---------------- 主线程命令 ----------------
    def cmd_move(self, n):
        v, clipped = clamp_count(int(n))
        with self.lock:
            self.target = v
            self.move_req = True
        if clipped:
            print(f'[软限位] 目标 {count2mm(int(n)):+.3f}mm 超出 [{SOFT_MIN_MM:g}, {SOFT_MAX_MM:g}]mm，已钳位到 {count2mm(v):+.3f}mm')

    def cmd_jog(self, n):
        raw = self.status['apos'] + int(n)
        v, clipped = clamp_count(raw)
        with self.lock:
            self.target = v
            self.move_req = True
        if clipped:
            print(f'[软限位] 相对移动后目标 {count2mm(raw):+.3f}mm 超出 [{SOFT_MIN_MM:g}, {SOFT_MAX_MM:g}]mm，已钳位到 {count2mm(v):+.3f}mm')

    def cmd_vel(self, n):
        with self.lock:
            if self.off_pv >= 0:
                self.vel = int(n)
                print(f'速度已设为 {count2mm(int(n)):.3f} mm/s')
            else:
                self.sdo_jobs.append(('wr', 0x6081, int(n)))

    def close(self):
        self.enable = False
        time.sleep(0.1)
        self.running = False
        time.sleep(0.1)
        try:
            self.master.state = pysoem.INIT_STATE
            self.master.write_state()
            self.master.close()
        except Exception:
            pass
        print('已安全退出')


def _desc(a) -> str:
    """pysoem 的 desc 可能是 bytes，统一转 str"""
    d = a.desc
    if isinstance(d, bytes):
        d = d.decode('utf-8', 'replace')
    return d or ''


def pick_adapter():
    adapters = pysoem.find_adapters()
    if not adapters:
        raise RuntimeError('没找到任何网卡（Npcap 是否已装？）')
    for i, a in enumerate(adapters):
        d = _desc(a)
        if 'intel' in d.lower() and 'wireless' not in d.lower() and 'bluetooth' not in d.lower():
            print(f'自动选择网卡[{i}]: {d}')
            return a.name
    for i, a in enumerate(adapters):
        print(f'  [{i}] {_desc(a)}')
    k = input('请输入网卡序号: ').strip()
    return adapters[int(k)].name


def stdin_thread(q):
    while True:
        try:
            q.put(input().strip())
        except EOFError:
            return


def main():
    if len(sys.argv) > 1:
        adapters = pysoem.find_adapters()
        print(f'使用网卡[{sys.argv[1]}]: {_desc(adapters[int(sys.argv[1])])}')
        ifname = adapters[int(sys.argv[1])].name
    else:
        ifname = pick_adapter()

    dev = Sv630n(ifname)
    dev.open()
    dev.running = True
    threading.Thread(target=dev.cyclic, daemon=True).start()

    q = queue.Queue()
    threading.Thread(target=stdin_thread, args=(q,), daemon=True).start()

    print('命令: e=使能 d=失能 r=复位 m=绝对定位 j=相对 v=速度 p=PDO s=状态 q=退出')
    print(f'例: m 50  绝对到50mm；j 10  正向10mm；v 5  速度5mm/s（导程 LEAD_MM={LEAD_MM}mm/圈）')
    print('★ 默认失能：先 e 使能（轴锁住），再 v 降速，再 j/m 动作；d=失能释放')
    print('★ 导程不对请改脚本顶部 LEAD_MM，位置/距离才会准')
    print(f'★ 软件限位：目标位置被钳位在 [{SOFT_MIN_MM:g}, {SOFT_MAX_MM:g}]mm（顶部 SOFT_MIN_MM/SOFT_MAX_MM 可改）')
    print('★ 读写参数: get 200d 1 读H0d.01 | set 200d 1 1 写H0d.01=1（十六进制索引）\n')
    def _fmt(s):
        st = s['sw'] & 0x006F
        if s['sw'] & 0x0008:
            tag = '故障'
        elif st == 0x27:
            tag = '到位' if s['sw'] & 0x0400 else '运动中'
        elif st == 0x23:
            tag = '待使能'
        elif st == 0x21:
            tag = '就绪'
        else:
            tag = '未使能'
        return f"SW:0x{s['sw']:04X} 位置:{count2mm(s['apos']):+8.3f}mm  {tag}{'' if s['wkc_ok'] else '  [WKC异常!]'}"

    last_print = 0.0
    last_sw = None
    last_apos = None
    while True:
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            line = None
        except KeyboardInterrupt:      # Ctrl+C 也走安全退出
            break
        if line:
            parts = line.split()
            c = parts[0].lower() if parts else ''
            try:
                if c == 'q':
                    break
                elif c == 'e':
                    dev.enable = True
                elif c == 'd':
                    dev.enable = False
                elif c == 'r':
                    dev.reset_fault = True
                elif c == 'm' and len(parts) > 1:
                    dev.cmd_move(mm2count(float(parts[1])))
                elif c == 'j' and len(parts) > 1:
                    dev.cmd_jog(mm2count(float(parts[1])))
                elif c == 'v' and len(parts) > 1:
                    dev.cmd_vel(mm2count(float(parts[1])))
                elif c == 'p':
                    print_pdo_map(dev.slave)
                elif c == 'set' and len(parts) >= 3:
                    idx = int(parts[1], 16)
                    sub = int(parts[2])
                    val = int(parts[3]) if len(parts) > 3 else 1
                    with dev.lock:
                        dev.sdo_jobs.append(('set', idx, sub, val))
                elif c == 'get' and len(parts) >= 2:
                    idx = int(parts[1], 16)
                    sub = int(parts[2]) if len(parts) > 2 else 0
                    with dev.lock:
                        dev.sdo_jobs.append(('get', idx, sub))
                elif c == 's':
                    print(_fmt(dev.status))
                else:
                    print('未知命令')
            except Exception as ex:
                print(f'命令错误: {ex}')

        # 状态仅在发生变化时打印（静止时完全安静，不干扰输入）
        s = dev.status
        sw, apos = s['sw'], s['apos']
        if sw != last_sw or apos != last_apos:
            last_sw, last_apos = sw, apos
            now = time.time()
            if now - last_print >= 0.5:      # 变化时最多每 0.5s 刷一行
                last_print = now
                print(_fmt(s))
    dev.close()


if __name__ == '__main__':
    main()
