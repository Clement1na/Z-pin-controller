# third-party —— 第三方文件

这里放的是**不是本团队写的**、但项目要用到的文件：
驱动程序、厂商手册、参考图、外部 Arduino 库。

原来它们散落在仓库根目录，和源码混在一起，交接时没人知道哪个是干什么的、要不要装、
能不能往外发。现在归到一处，逐项说明如下。

> **一句话原则**：这个目录里的东西**都不参与 Python 代码运行**（唯一的例外是
> `AccelStepper/`，它是**固件**编译时才需要的）。删掉整个目录，PC 端代码照样能跑；
> 但装不了驱动 / 编不了固件。

---

## 目录一览

```
third-party/
├── drivers/          驱动安装包（需要手动装）
│   ├── CH341SER.exe      USB 转串口驱动
│   └── npcap-1.89.exe    EtherCAT 前置驱动 ★ 必装
├── docs/
│   └── SV630N系列伺服用户手册-CN-D00.PDF    汇川官方手册
├── AccelStepper/     Arduino 步进电机库（固件编译依赖）
└── hardware/         接线参考图
    ├── cnc-shield-pinout.jpg
    └── esp32-firebeetle2-pinout.png
```

---

## `drivers/CH341SER.exe`

| | |
|---|---|
| **是什么** | 沁恒（WCH）CH340/CH341 USB 转串口芯片的 Windows 驱动 |
| **谁写的** | 沁恒微电子（WCH），厂商官方驱动 |
| **要不要装** | **看情况**——如果 ESP32 开发板用的 USB 串口芯片是 CH340 系列，就必须装；否则设备管理器里看不到串口 |
| **许可** | 厂商免费提供的驱动，可自由安装使用。官网 <https://www.wch.cn/downloads/CH341SER_EXE.html> |
| **详细安装步骤** | [ENVIRONMENT.md 第 4 节](../ENVIRONMENT.md) |

**怎么判断需不需要**：插上 ESP32，看设备管理器 → 端口里出现的是
`USB-SERIAL CH340 (COMx)`（需要）还是别的名字（如 CP2102、FT232，那就要装**对应厂商**的驱动，
本仓库里没有）。

---

## `drivers/npcap-1.89.exe`

| | |
|---|---|
| **是什么** | Npcap —— Windows 上的**内核级网络抓包驱动** |
| **谁写的** | Nmap Project（nmap.org），Npcap 的发布方 |
| **要不要装** | ★ **必须装**。EtherCAT 链路（变距伺服）完全依赖它，不装伺服一行代码都跑不了 |
| **许可** | ⚠️ **Npcap Free Edition，EULA 禁止再分发**，且对安装台数有限制。分发条款以 <https://npcap.com/> 上的 LICENSE 原文为准 |

### ⚠️ 两条硬性注意

1. **安装时必须勾选 `Install Npcap in WinPcap API-compatible Mode`**，
   否则 pysoem 运行期会报找不到 `wpcap.dll`。必须以**管理员身份运行**。
2. **不要把 `npcap-1.89.exe` 打进交付包发给别人。** 免费版许可不允许再分发。
   交付给终端用户时，放一页说明 + 官网下载链接
   <https://npcap.com/#download>，让用户自己装。
   商业交付或要装到多台机器，需向 Nmap 项目购买 OEM Redistribution 授权。

详细安装步骤见 [ENVIRONMENT.md 第 5 节](../ENVIRONMENT.md)。

---

## `docs/SV630N系列伺服用户手册-CN-D00.PDF`

| | |
|---|---|
| **是什么** | 汇川 SV630N 系列伺服驱动器的官方用户手册（中文，版本 CN-D00） |
| **谁写的** | 汇川技术（Inovance），厂商官方文档 |
| **要不要装** | 不用装，**查阅用**。调参数、看对象字典、排故障码都要翻它 |
| **许可** | 版权归汇川技术所有，随产品提供的技术资料。**不要对外分发**，需要的话让接收方从汇川官网或代理商处获取 |

`servo/sv630n_pc.py` 里的 PDO 映射（手册 11.7.2）、对象字典索引
（`6040h`/`6041h`/`6060h`/`6064h`/`607Ah`/`6081h` 等）都是照这本手册写的。

> 顺带一提：这台设备的驱动器是**汇川 SV630N**，不是台达的。SV630N 的
> CN3/CN4 是 **EtherCAT 专用口，不提供 Modbus RTU/RS485**——带 RS485 的是另一个型号
> **SV630C**。所以控制它**只能走 EtherCAT**，没有退路。

---

## `AccelStepper/`（版本 1.64）

| | |
|---|---|
| **是什么** | Arduino 步进电机控制库 |
| **谁写的** | Mike McCauley（airspayce.com） |
| **要不要装** | **编译固件时需要**——`firmware/insert_head/insert_head.ino` 里有 `#include <AccelStepper.h>` |
| **许可** | **GPL V3 或商业授权双许可**（见目录下 `LICENSE`）。原话是 "The main licensing options available are GPL V3 or Commercial" |

### 怎么装

把 `third-party/AccelStepper/` **整个文件夹**拷进 Arduino 的库目录
（一般是 `文档\Arduino\libraries\`），或者压缩成 zip 后用
`项目 → 加载库 → 添加 .ZIP 库`。详见 [ENVIRONMENT.md 第 9 节](../ENVIRONMENT.md)。

### ⚠️ 许可提醒

AccelStepper 是 **GPL V3**（或商业授权）。GPL V3 有传染性：
如果你的固件要用它**并且对外分发**，按 GPL V3 的条款你可能需要一并公开固件源码。
不想受这个约束就得向作者买商业授权。

这条**目前不影响内部使用**（自己用不分发），但如果将来要把设备/固件卖出去或开源，
需要先想清楚这件事。

---

## `hardware/` —— 接线参考图

| 文件 | 内容 |
|---|---|
| `cnc-shield-pinout.jpg` | Arduino **CNC Shield** 扩展板的针脚定义图（原文件名 `arduino扩展板.jpg`） |
| `esp32-firebeetle2-pinout.png` | **ESP32 FireBeetle 2** 开发板的针脚图（原文件名是一串哈希 `eb991fc5….png`） |

- **用途**：烧录固件、核对接线时对照用。`insert_head.ino` 顶部的
  `STEP_PIN`/`DIR_PIN`/`EN_PIN`/`VALVE_A_PIN`/`VALVE_B_PIN` 就是按这些板子的针脚定的。
- **来源**：**不清楚**。原文件名（`arduino扩展板.jpg` 和那串哈希 png）没有保留出处信息，
  推测是从网上搜集或卖家提供的资料。**如果要正式对外发布这个仓库，
  建议先确认这两张图的来源和授权**，或者干脆删掉——它们只是参考，不影响运行。
- **许可**：来源不明，无法确认。**仅内部参考，不要对外分发。**

---

## 要交付给用户时，这个目录怎么处理？

| 文件 | 交付包里放不放 |
|---|---|
| `drivers/npcap-1.89.exe` | ❌ **不放**（许可禁止再分发）。改为在说明里给官网链接 |
| `drivers/CH341SER.exe` | ✅ 可以放（厂商免费驱动），也可以给官网链接 |
| `docs/SV630N…PDF` | ❌ **不放**（汇川版权） |
| `AccelStepper/` | ✅ 放（如果连固件源码一起交付），并按 GPL V3 附上 `LICENSE` |
| `hardware/*.jpg,*.png` | ⚠️ 来源不明，**先查清来源再决定** |
