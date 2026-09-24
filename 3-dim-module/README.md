# cnc-grbl

通过串口向 ESP32 Grbl / FluidNC 发送 G 代码。默认 `COM9`，`115200`。

## 内容

- `src/cnc_grbl/`：可导入的控制库
- `send.py`：命令行测试程序（发你输入的内容，打印回读）

## 安装

Python 3.10+。

```bash
pip install -e .
```

或只装依赖后直接跑测试脚本：

```bash
pip install pyserial
python send.py
```

## 测试程序

```bash
python send.py
```

连上后输入 G 代码回车即可。只发一条：

```bash
python send.py '$I'
```

PowerShell 里 `$` 开头的命令必须用单引号。COM9 不能被其它串口软件占用。

## 库用法

```python
from cnc_grbl import GrblController

with GrblController(port="COM9") as cnc:
    cnc.unlock()
    cnc.send("G21 G90")
    cnc.send("G0 X10 Y10 F500")
    print(cnc.status())
```
