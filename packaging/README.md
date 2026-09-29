# Z-pin 五针植入控制 —— 交付包说明

这个文件夹是 PyInstaller 的构建输入。构建产物在 `dist\ZpinControl\`，那个文件夹
整个拷走就能用，目标机器上**不需要**装 Python、conda 或本仓库的源码。

## 构建

```powershell
powershell -File packaging\build.ps1
```

换 Python 解释器：

```powershell
powershell -File packaging\build.ps1 -Python C:\Python313\python.exe
```

默认执行策略下就能跑，不需要动 `-ExecutionPolicy`。只有当本机策略确实禁止运行
脚本（报 `无法加载文件……因为在此系统上禁止运行脚本`）时，才需要你自己决定是否
放行；不要在没看懂那条策略是干什么的情况下随手加 `Bypass`。

构建脚本会做三件事：跑 PyInstaller、把 `master\config.toml` 模板复制成 exe 旁边的
`config.toml`（**已存在就不覆盖**，免得抹掉现场量出来的串口号和软限位）、把本文件
复制成 `使用说明.md`。

验收（改完代码或换机器后都该跑一遍）：

```
cd dist\ZpinControl
.\ZpinControl.exe --smoke-test
```

期望输出 `已完成：18 个落点，共 3 排（每排 6 个）` 和 `阀门已关闭`，退出码 0。
这条命令不读配置、不开任何设备。

## 产物结构

```
dist\ZpinControl\
  ZpinControl.exe     ← 入口，和源码 CLI 是同一个 master/master.py
  config.toml         ← 外部可编辑配置，装完必须改
  config.local.toml   ← 向导生成，存本机串口/网卡；首次运行前不存在
  runs\               ← 每次实机运行的 CSV 和 JSON 摘要；首次运行后出现
  _internal\          ← PyInstaller 的运行时载荷，别动
  使用说明.md          ← 本文件
```

`config.toml` 是**模板**，里面 `[job]` 段是注释掉的，`[servo] adapter` 和两个
`port` 是空的 —— 这是故意的：`--live` 在补齐实测值和工件零点之前会在打开硬件
**之前**就拒绝运行。

## 目标机器上要装什么

只有一个外部依赖：

- **Npcap** —— EtherCAT 走网卡抓包，pysoem 依赖它。从 <https://npcap.com/#download>
  下载安装。安装时勾选 "WinPcap API-compatible Mode"。

  本包**不分发** Npcap 安装程序：它的免费授权禁止再分发。请到官网自取。

pyserial 已经打进包里了，CH340 串口驱动（GX28 滑台和 CNC 三维模组用）是 Windows
自带或随设备附带的，本包也不含。

## 怎么用

### 首次配置

1. 装 Npcap。
2. 在终端里运行 `ZpinControl.exe`，不带任何参数 → 进菜单 → 选 `[1] 实机运行`。
   向导会枚举串口和网卡、让你逐项确认，然后写出 `config.local.toml`。
3. 之后每次开机，向导仍会让你确认一遍待机位置和零点 —— 断电重启后滑台和伺服都
   把当前点当作零位，那不代表机械零位。

### 菜单

不带参数在终端里运行时进菜单：

```
  [1] 实机运行（会连接并可能移动机构）
  [2] 仿真（不接硬件）
  [3] 自检（参考作业 360×360/4/5）
  [4] 调试输出：关
  [0] 退出
```

全程用编号选择，只有在必须读数值（作业尺寸、点动距离）时才要求输入。
`[4]` 是开关，改完回到菜单，不退出。

### 调试输出

菜单里把 `[4]` 打开，或者命令行加 `--debug`：

```
ZpinControl.exe --live --confirm-datum --datum-x 0 --datum-z -5 --debug
```

`--datum-x` / `--datum-z` 是对刀点，**每次装夹都要重新量**，上面的数值只是占位。
非交互路径必须显式给出，少一个就会在连接设备之前报错退出——不带 `--confirm-datum`
走向导时不用填，向导会让你对刀两次把坐标读回去。

打开后，设备自己说的每一行都原样打到终端，出错时**固件原话先出现，主控的转述
随后**：

```
[GX28] > -5.000
[GX28] < 移动到 -5.00 mm (16000 步)...
[GX28] < 到达目标
[CNC ] > G1 X10.000 Y20.000 Z0.000 F1500
[CNC ] < ok
[伺服] > 绝对定位 +0.000 mm
[伺服] < 状态字=0x0237 到位
```

CNC 每次移动都会每 0.1 秒发一次 `?` 查询状态，一秒 10 行一模一样的回话。那些是心
跳不是设备主动说的话，所以只在**状态变化**时打一次；`ok`、`error:N`、`ALARM:N`
一律原样打印。

关闭调试（默认）**不等于少看东西**：设备报的错一个字不少地传上去，只是不再回显
正常回话。

### 命令行的三种模式

菜单不是唯一入口，脚本化调用继续走参数：

```
ZpinControl.exe --smoke-test                          # 参考作业全流程，不接硬件
ZpinControl.exe --simulate --angle -30 --length 20 --spacing 2 --depth 3   # 指定作业仿真
ZpinControl.exe --live --confirm-datum --datum-x 0 --datum-z -5 --debug    # 实机
```

`--smoke-test` / `--simulate` / `--live` 三者互斥。在**非终端**环境（管道、重定向、
计划任务）里不带模式参数运行会直接报用法错误，不会挂在那里等输入。

## 重新打包时要留意

- `packaging\ZpinControl.spec` 里的 `pathex` 不能删：`sv630n_pc` 和 `cnc_grbl` 是
  **运行期**才 `sys.path.insert` 进去的惰性导入，PyInstaller 在构建期看不到那两条
  路径，删了就会在实机连伺服/CNC 时才炸 ImportError。
- 必须是 `console=True` 的 onedir。`main()` 用 `sys.stdin.isatty()` 决定要不要给
  菜单，无控制台窗口时 `sys.stdin` 是 `None`。
- 排除了 `unittest` / `tkinter` 等用不到的栈；如果以后有模块真的要用，从
  `excludes` 里删掉再加回去。
