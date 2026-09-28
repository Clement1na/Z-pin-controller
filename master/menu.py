# -*- coding: utf-8 -*-
"""运行期菜单：把"必须记住命令行参数"变成编号选择。

这里放两样东西：顶层启动菜单，以及运行期共用的交互引擎 `make_menu()`。
运行中的安全设置菜单在 `master.master` 里，两者用同一个引擎。

菜单是**新增入口**，不是替换：`--smoke-test` / `--simulate` / `--live`
三个参数原样保留，脚本化调用和自动化测试继续走参数，不必经过菜单。
"""
from __future__ import annotations

from pathlib import Path


def make_menu(*, input_fn=None, output=None):
    """运行期交互引擎。

    复用 `Wizard`：它是仓库里唯一带输入接缝、能被无 TTY 测试驱动的交互实现。
    `input_fn` 留空时由 `Wizard` 在构造那一刻取内建 `input`，所以
    `patch("builtins.input")` 对它同样有效。
    """
    from master.wizard import Wizard
    return Wizard(input_fn=input_fn, output=output)


def run_startup_menu(*, config_path, results_dir=None, debug=False, menu=None,
                     wizard_fn=None, simulate_fn=None) -> int:
    """没有给模式参数、且在终端里直接运行时进入这里。

    选定一项就执行并返回它的退出码，**不弹回菜单**：实机跑完机器处于需要人工
    处理的状态，自动再来一轮是危险的默认值。
    """
    from master.master import _known_geometry, run_simulation
    from master.model import JobSpec
    from master.wizard import WizardCancelled, collect_job, run_setup_wizard

    menu = make_menu() if menu is None else menu
    wizard_fn = run_setup_wizard if wizard_fn is None else wizard_fn
    simulate_fn = run_simulation if simulate_fn is None else simulate_fn
    debug_on = debug
    while True:
        menu.say("")
        menu.say("Z-pin 五针植入控制")
        try:
            choice = menu.choose("选择", (
                ("1", "实机运行（会连接并可能移动机构）"),
                ("2", "仿真（不接硬件）"),
                ("3", "自检（参考作业 360×360/4/5）"),
                ("4", f"调试输出：{'开' if debug_on else '关'}"),
                ("0", "退出"),
            ))
        except WizardCancelled:
            return 0
        if choice == "4":
            # 切换项：改完重绘菜单，不退出。
            debug_on = not debug_on
            menu.say(f"调试输出已{'开启' if debug_on else '关闭'}。")
            continue
        if choice == "0":
            return 0
        if choice == "1":
            return wizard_fn(Path(config_path), results_dir=results_dir, debug=debug_on)
        if choice == "2":
            job, _ = collect_job(menu, _known_geometry())
            return simulate_fn(job)
        return simulate_fn(JobSpec(360.0, 360.0, 4.0, 5.0))
