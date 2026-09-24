# z-pin-control

Z-pin（复合材料层间增强销钉）植入设备的控制代码：**PC 端总控 + 三块下位机固件**。

一台设备上其实并排跑着**四条互不相干的控制链路**，各有各的通信协议，PC 侧用一个命令行总控
(`master/master.py`) 把它们串起来。本仓库就是这四条链路的全部代码。

> 第一次拿到这个仓库请先看 **[ENVIRONMENT.md](ENVIRONMENT.md)**——环境有一处硬性约束
> （Python 必须是 3.13）和一个必须单独安装的内核驱动（Npcap），不看会卡住。

---

## 1. 硬件拓扑

| # | 执行器 | 干什么 | 下位机 | 通信通道 | PC 端代码 |
|---|---|---|---|---|---|
| 1 | **变距模组** | 调针距（改变植入点间距） | 汇川 SV630N 伺服驱动器 | **EtherCAT**，PC 网口直连驱动器 CN3(IN) | `servo/sv630n_pc.py`（pysoem） |
| 2 | **三维模组 X/Y/Z** | 移动工件到下一个植入点 | ESP32 + Grbl / FluidNC | **串口** `COM9` @115200，G 代码 | `cnc3d/`（`cnc_grbl` 包） |
| 3 | **植入滑台 + 双气阀** | 下压植入销钉 / 退回，气阀开关 | ESP32 + CNC Shield + A4988 + AccelStepper | **串口** `COM7` @115200，自研单行协议 | `master/devices.py` ↔ `firmware/insert_head/insert_head.ino` |
| 4 | *（孤儿：45° 并联机构）* | *16 点位定位，历史遗留* | ESP32 | 串口 @9600，单字符 `a`–`p` | **无**（没有任何 PC 代码调用）↔ `firmware/legacy/cnc2d_parallel/` |

链路 1 是**实时以太网**，第 2、3 条是**两条独立串口**，第 4 条**没有接入总控**。
第 3 条的协议是**两端各写一遍**的：PC 侧 `master/devices.py` 与板子侧 `insert_head.ino` 必须同时改才一致。

### 三条链路的关系

```
                       ┌──────────────────────────────┐
                       │   master/master.py  (CLI)     │
                       └───┬────────┬─────────────┬───┘
                           │        │             │
              ┌────────────▼──┐  ┌──▼──────────┐ ┌▼─────────────────┐
              │ ServoAxis     │  │ Cnc3D       │ │ InsertHead       │
              │ (devices.py)  │  │ (devices.py)│ │ (devices.py)     │
              └────────┬──────┘  └──┬──────────┘ └┬─────────────────┘
                       │            │             │
        ┌──────────────▼──┐  ┌──────▼───────┐  ┌──▼──────────────────┐
        │ servo/          │  │ cnc3d/       │  │ 串口 COM7 自研协议   │
        │ sv630n_pc.py    │  │ cnc_grbl 包  │  │ ↕ 必须与固件同步     │
        │ (pysoem)        │  │ (pyserial)   │  │ firmware/insert_head│
        └────────┬────────┘  └──────┬───────┘  └─────────────────────┘
                 │ EtherCAT         │ 串口 COM9
        ┌────────▼────────┐  ┌──────▼─────────┐
        │ SV630N 伺服     │  │ ESP32 Grbl     │
        └─────────────────┘  └────────────────┘
```

`devices.py` 里所有 `import` 都写在**函数内部**（惰性导入），所以缺 pysoem 或没插三维模组时，
其它链路照样能用——这也是为什么三块板子可以分别调试。

---

## 2. 目录索引

```
z-pin-control/
├── README.md                    本文件：这设备是什么、怎么跑
├── ENVIRONMENT.md               ★ 从零搭环境：Python/conda/驱动/网卡/串口/烧录/标定/排错
├── requirements.txt             pip 依赖（pysoem==1.1.13 / pyserial>=3.5）
├── environment.yml              conda 环境定义（python=3.13 是硬要求）
├── .gitignore  .gitattributes
│
├── master/                      PC 端总控（原 implant_master/）
│   ├── master.py                命令行主程序，串起三条链路
│   ├── devices.py               三个执行器的统一封装 + mm/count 换算
│   └── config.toml              ★ 串口号、导程、软限位、流程参数都在这里
│
├── servo/                       EtherCAT 变距伺服
│   └── sv630n_pc.py             SV630N 驱动类 + 独立调试 CLI（也可单独运行）
│
├── cnc3d/                       三维模组（原 3-dim-module/）
│   ├── src/cnc_grbl/            可导入的控制库（后台线程收串口 + 队列）
│   ├── send.py                  命令行测试小程序
│   ├── pyproject.toml  README.md
│
├── firmware/                    下位机固件（Arduino sketch）
│   ├── insert_head/insert_head.ino        植入滑台 + 双气阀（原 Untitled-1.cpp）
│   └── legacy/cnc2d_parallel/             45° 并联机构，未接入总控，见其 README
│
├── scripts/                     启动器
│   ├── _python.bat              统一探测 Python 解释器（被下面两个 call）
│   ├── start-master.bat         启动总控
│   └── start-servo-debug.bat    单独调试变距伺服
│
└── third-party/                 第三方文件，见其 README
    ├── drivers/                 CH341SER.exe（串口驱动）、npcap-1.89.exe（EtherCAT 前置驱动）
    ├── docs/                    SV630N 伺服用户手册 PDF
    ├── AccelStepper/            Arduino 步进电机库 1.64（固件依赖）
    └── hardware/                CNC Shield 针脚图、ESP32 开发板针脚图
```

**根目录再无散落的 `.py` / `.cpp`**：每个条目都是目录，每个目录都有明确用途。

---

## 3. 快速开始

```bash
# 1) 建环境（详见 ENVIRONMENT.md；Python 必须是 3.13）
conda env create -f environment.yml
conda activate zpin

# 2) 改串口号：打开 master/config.toml，把 COM9 / COM7 改成设备管理器里实际的号
#    并显式填上 [servo] adapter（网卡名，别留空——见"已知问题 3"）

# 3) 启动总控
python master/master.py

# 或直接双击（会自动找解释器，不用配 PATH）
scripts\start-master.bat
```

只想单独调变距伺服（不开其它两条链路）：

```bash
python servo/sv630n_pc.py          # 会列出可用网卡让你选
scripts\start-servo-debug.bat 1    # 直接指定网卡序号
```

---

## 4. 总控命令表

连上以后在 `> ` 提示符下输入（`master/master.py` 的 `HELP`）：

| 命令 | 作用 | 命令 | 作用 |
|---|---|---|---|
| `c` | 连接全部设备 | `cs` / `c3` / `ch` | 单独连 伺服 / 三维 / 滑台 |
| `d3 x y z` | 三维模组绝对移动 mm | `h3` / `x3` | 三维回零 `$H` / 解锁 `$X` |
| `a` | 执行单点植入流程 | `r` | 三维模组回到 (0,0,0) |
| `p <mm>` | 变距伺服绝对移动 | `j <mm>` | 变距伺服相对移动 |
| `v <mm>` | 滑台绝对移动 | `z` | 滑台对零（当前位置 = 0） |
| `e` / `d` | 滑台使能 / 释放 | `a1 a0 b1 b0` | 气阀手动开关 |
| `s` | 全部状态 | `!` | **急停**（全停 + 关阀） |
| `?` | 打印帮助 | `x` / `quit` | 退出（安全关闭） |

`!` 急停会：停滑台 → 关两个气阀 → 三维模组 hold → 伺服 disable。

---

## 5. 一次完整植入的时序

`a` 命令（`master.py` 的 `cycle_one()`）走六步，参数取自 `config.toml` 的 `[cycle]`：

```
1. 开阀 A          ← 夹紧 / 送出销钉
2. 滑台下行        移动 down_mm（默认 5.0mm，坐标减小为"下"）
   └ 目标位置 = max(0, min(travel_mm, 当前位置 - down_mm))   ← 滑台软限位
3. 关阀 A
4. 开阀 B
5. 滑台上行        移动 up_mm（默认 5.0mm，退回）
6. 关阀 B
```

每步之间按 `valve_delay_ms`（默认 500ms）等待气阀/气缸动作到位。

**注意**：板子固件里还有一份 `autoImplant()` 实现了同样的流程，**PC 从不调用它**。
改流程时两边都要看（见"已知问题 4"）。

---

## 6. 已知问题（**只记录，本次整理未动**）

这些是通读代码时发现的真实隐患，逐条列出以免交接时踩坑。**修它们需要实机标定或改逻辑，
超出了本次"目录整理"的范围。**

1. **导程有两份定义，且相差 13.33 倍**（*最需要注意的一条*）
   - `servo/sv630n_pc.py` 里 `LEAD_MM = 13.3333333`
   - `master/config.toml` 里 `lead_mm = 1.0`
   - `ServoAxis._mm2count`（`devices.py:51`）用的是 **config 的 1.0**，而
     `Sv630n.clamp_count` 的软限位钳位用的是脚本里的 **13.3333**。两者不同源，
     导致 `p -5` 会被钳到 -10mm、且可用行程被压缩到 0.75 圈电机。
   - 结论：**必须先实测标定导程**（方法见 ENVIRONMENT.md 第 9 节），然后**只保留一份定义**。

2. **`j` 相对移动绕过软限位**：`devices.py:76` 直接写 `self.dev.target`，
   没走 `clamp_count`，所以相对移动可以跑出软限位。

3. **`pick_adapter()` 会误选 Wi-Fi 网卡**：`sv630n_pc.py:339` 的判据是
   "描述里有 `intel`，且没有 `wireless`/`bluetooth`"。而本机无线网卡叫
   `Intel(R) Wi-Fi 6 AX201`——**描述里恰好不含 "wireless" 这个词**，会被误选。
   **对策：`config.toml` 里显式填 `adapter`，不要留空。**

4. **植入流程有两份实现**：板载 `insert_head.ino` 的 `autoImplant()` 与 PC 端
   `cycle_one()`，参数（5mm/500ms）各写一遍。当前 PC 不用板载那份。

5. **实时线程里做 SDO 读**：`sv630n_pc.py` 的 `cyclic()` 在 2ms 周期里发 SDO 请求，
   会给周期任务引入抖动。EtherCAT 上 SDO 应该挪到非周期上下文。

6. **靠中文打印文案判断应答**：`devices.py` 的 `InsertHead._read_until` 匹配的是
   `"到达目标"`、`"气阀A 打开"` 这类中文字符串。**改固件里的任何 `Serial.print` 文案
   都会让 PC 端超时**——这是隐式的强耦合。

7. **`sv630n_pc.py:5` 的注释过时**：写着"依赖 Python 3.12"，实测 3.12 装不上 pysoem
   （只有 cp313 有 Windows wheel）。见 ENVIRONMENT.md。

---

## 7. 相关文档

- **[ENVIRONMENT.md](ENVIRONMENT.md)** — 环境搭建、驱动、网卡、串口、烧录、标定、排错
- **[third-party/README.md](third-party/README.md)** — 每个第三方文件的用途、来源、许可
- **[cnc3d/README.md](cnc3d/README.md)** — 三维模组串口库自己的说明
  （它写的是"Python 3.10+"，那是**这个子包单独用**时的要求，它只依赖 pyserial。
  整个项目跑起来仍是 **3.13**，因为伺服那条链路要 pysoem）
- **[firmware/legacy/cnc2d_parallel/README.md](firmware/legacy/cnc2d_parallel/README.md)** — 那条孤儿链路的说明
