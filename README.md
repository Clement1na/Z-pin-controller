# Z-pin 五针金属丝植入机控制系统

本项目用于科研实验中的碳纤维板金属丝植入。两轴横向扫描与工作高度由改装的 TwoTrees TTC450 Pro 三维模组执行；五针工位间距由 SV630N EtherCAT 伺服变距模组执行；GX28/2830 滑台和上下两个气阀完成夹丝、下针、换夹、退针。历史的 45° 并联机构放在 `firmware/legacy/`，不参与现流程。

`v0.1` Git 标签保存了目录整理后的原始基线。当前 `develop/v0.2` 分支修复与验证总控，详细顺序见 [实施计划](docs/superpowers/plans/2026-09-24-zpin-control-to-exe.md)，逐步证据见 [开发日志](DEVELOPMENT_LOG.md)。

## 已实现的离线流程

用户提供板厚、矩形植入范围和点间距。排程器从五针的 `10–20 mm` 针距中选择一个可完整覆盖 X 行的整数倍针距。每次五针同时植入，TTC450 Pro 在相邻落点间扫描，逐行移动 Y。参考作业采用半开区域 `[0,360) × [0,360)` mm、4 mm 点距、5 mm 板厚，共 90×90 = 8100 点；针距 12 mm、伺服目标 −2 mm、每行 18 次、总计 1620 次五针动作。

每一落点的顺序为：下阀 A 夹丝 → GX28 下行 → 关闭 A → 上阀 B 夹丝 → GX28 上行 → 关闭 B。此处保留昨天在设备上验证过的“先下、后上”方向。滑台下行对应软件坐标减小；板厚本身不等于滑台行程。滑台待机坐标、下行目标与有效工作区间必须分别测定。

所有模式使用 `master/planner.py` 和 `master/coordinator.py`。模拟器替换设备接口，不重写排程或动作时序。故障与停止会阻止后续落点，并尝试停机和关闭两个阀。

## 源码运行与测试

开发环境用 Python 3.13、`pysoem`、`pyserial`；本机 `zpin` 环境可用。Npcap 已安装并运行，但此电脑没有接入三路实机。避免用户站点包干扰时在 PowerShell 中运行：

```powershell
$env:PYTHONNOUSERSITE = '1'
& 'D:\miniconda3\envs\zpin\python.exe' -B master/master.py --smoke-test
& 'D:\miniconda3\envs\zpin\python.exe' -B -m unittest discover -s tests -v
```

`--smoke-test` 完整执行参考作业，不枚举或打开任何网卡/串口。`--smoke-test --inject-failure` 验证首次下行故障的收尾和非零退出码。自定离线作业可运行：

```powershell
& 'D:\miniconda3\envs\zpin\python.exe' -B master/master.py --simulate --width 360 --height 360 --spacing 4 --thickness 5
```

实机入口为 `master/master.py --live --config master/config.toml --confirm-datum`。模板刻意缺少实测数据；配置不完整时，程序在连接设备之前报错。实机接入前须确认串口/有线网卡、CNC 工作零点与安全扫描 Z、GX28 烧录版本及其软件坐标、可用工作窗口、阀门释放规则和材料/丝材准备流程。运行时输入 `!` 请求停止。不能以离线测试代替实机试运行。

## 当前待核对的硬件事实

- 昨天成功执行“先下、后上”时，ESP32 烧录的 `.ino` 版本、执行前 `s` 位置、是否执行过 `z`。仓库旧固件将下行写成负坐标，但把目标钳在 `0–50 mm`；从软件零位 0 下行会变成原地不动。打开串口还可能让 ESP32 复位其软件坐标。
- 板厚 5 mm 对应的针尖与板面间隙、实际植入深度/露出长度、GX28 丝杆导程和细分、可重复的手动基准。
- 上阀 B 退针后是否释放，以及下一落点如何供丝/切丝。当前离线样例按仓库旧流程在退针后关闭 B；真实运行要求明确确认。
- TTC450 Pro 实际 X/Y/Z 工作限位、哪个针头定义 X=0、360 mm 边界是否包含。当前排程采用不包含终点的区域；无法完整五针覆盖的边缘输入会报错。

环境、串口、Npcap 和独立调试脚本见 [ENVIRONMENT.md](ENVIRONMENT.md)。最终交付目标是带文件夹的 Windows EXE，用户无需安装 Python/conda，但首次仍须单独安装 Npcap。打包安排在源码流程和无硬件 smoke test 之后。
