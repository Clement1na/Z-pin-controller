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
                     wizard_fn=None, simulate_fn=None, profile_manager_fn=None,
                     manual_fn=None) -> int:
    """没有给模式参数、且在终端里直接运行时进入这里。

    选定一项就执行并返回它的退出码，**不弹回菜单**：实机跑完机器处于需要人工
    处理的状态，自动再来一轮是危险的默认值。

    跑完会停住等回车。双击 exe 启动时控制台是新的，进程一退窗口就关，操作员
    连结果都来不及看——停顿只加在这条交互路径上；`--smoke-test` / `--simulate`
    这类脚本化调用不受影响，否则自动化会挂在那里等输入。
    """
    from master.master import SMOKE_JOB, SMOKE_ROWS, _known_geometry, run_simulation
    from master.model import RowSpec
    from master.profile_ui import run_profile_manager
    from master.profiles import ProfileStore
    from master.wizard import WizardCancelled, collect_job, run_setup_wizard

    menu = make_menu() if menu is None else menu
    wizard_fn = run_setup_wizard if wizard_fn is None else wizard_fn
    simulate_fn = run_simulation if simulate_fn is None else simulate_fn
    profile_manager_fn = run_profile_manager if profile_manager_fn is None else profile_manager_fn
    store = ProfileStore(Path(config_path).parent / "profiles")
    selection = store.load_selection()
    debug_on = debug
    while True:
        menu.say("")
        menu.say("Z-pin 五针植入控制")
        try:
            choice = menu.choose("选择", (
                ("1", "实机运行（会连接并可能移动机构）"),
                ("2", "仿真（不接硬件）"),
                ("3", f"自检（参考作业 θ=+{SMOKE_JOB['angle_deg']:g}° / 强化区 "
                      f"{SMOKE_JOB['length_mm']:g} / 点距 {SMOKE_JOB['spacing_mm']:g} / "
                      f"{SMOKE_ROWS} 排）"),
                ("4", f"调试输出：{'开' if debug_on else '关'}"),
                ("5", f"配置档案（机器：{selection['machine'] or '未选择'}；"
                      f"作业：{selection['job'] or '未选择'}）"),
                ("6", "组件手动控制"),
                ("0", "退出"),
            ))
        except WizardCancelled:
            return 0
        if choice == "4":
            # 切换项：改完重绘菜单，不退出。
            debug_on = not debug_on
            menu.say(f"调试输出已{'开启' if debug_on else '关闭'}。")
            continue
        if choice == "5":
            selection = profile_manager_fn(store, menu, Path(config_path), _known_geometry())
            continue
        if choice == "0":
            return 0
        if choice == "1":
            code = wizard_fn(Path(config_path), results_dir=results_dir, debug=debug_on,
                             profile_store=store, profile_selection=selection)
        elif choice == "2":
            # 参数问答也必须接住取消。`menu.choose` 上面裹了，这里不裹的话 `Q`/Ctrl-C
            # 会让 WizardCancelled 一路逃出 main 的 except 元组，未捕获异常直接结束进程
            # ——双击启动时就是窗口一闪而过，操作员看不到任何解释。
            try:
                row, _ = collect_job(menu, _known_geometry())
            except WizardCancelled as exc:
                menu.say(f"已取消（{exc}），未开始仿真；回到菜单。")
                continue
            code = simulate_fn(row)
        elif choice == "3":
            code = simulate_fn(RowSpec(**SMOKE_JOB), rows=SMOKE_ROWS)
        else:
            if manual_fn is None:
                menu.warn("组件手动控制尚未接入。")
                continue
            code = manual_fn(store=store, selection=selection,
                             config_path=Path(config_path), debug=debug_on)
        _hold_before_exit(menu)
        return code


def _hold_before_exit(menu) -> None:
    """停住等回车，让操作员看清屏幕再让窗口关闭。

    走 `menu.ask` 是为了复用同一个输入接缝：测试能喂一个回车进来。回车以外的
    任何反应（`Q`、Ctrl-C、stdin 已关闭）都当作"知道了，走吧"——这一步是礼貌，
    不是安全联锁，不该因为它再抛一次异常把进程带走。
    """
    from master.wizard import WizardCancelled

    menu.say("")
    try:
        menu.ask("按回车退出")
    except WizardCancelled:
        pass
