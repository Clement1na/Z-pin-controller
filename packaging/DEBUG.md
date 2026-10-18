# Z-pin 调试版

双击 `ZpinControlDebug.exe` 打开原来的完整调试界面：接口与机器配置、实机运行、仿真、自检、配置档案、预设任务、完全自定义；可开启设备通信回显。

用户版与调试版分别位于 `ZpinControl/` 和 `ZpinControlDebug/`，各自保留配置、命名档案与实验记录。首次生成调试包时复制当前机器设置，此后重打包保留各自最新数据。不同工件的对刀点不可混用。

调试版保留原来的独立CNC排间安全Z设置。用户版以GX28固定15 mm抬针保护移动，省略手动设置安全Z；两版都保留沿斜面的CNC Z跟随、深度/cosθ换算及对刀位上方1.5 mm套丝。

`ZpinControl.exe` 为命令行入口。不连接硬件的自检：

```powershell
.\ZpinControl.exe --smoke-test
```

预期12次植入动作、3排、阀门关闭、退出码0。实机与完全自定义会连接并可能驱动机构；上电显示0不能证明机械零位。

本次改动前的完整旧包保存在开发项目 `build/rollback-before-user-ui-20261018`，源码备份为 `build/source-before-user-ui-20261018.zip`。
