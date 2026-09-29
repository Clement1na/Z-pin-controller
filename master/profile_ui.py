"""Interactive profile selection and creation for the CLI menu."""

from __future__ import annotations

from pathlib import Path

from master.local_config import load_merged
from master.profiles import ProfileNameError, ProfileStore
from master.wizard import WizardCancelled, collect_job, job_values


def _select_existing(store: ProfileStore, menu, kind: str,
                     current: str | None) -> str | None:
    names = store.list_names(kind)
    label = "机器配置" if kind == "machines" else "作业方案"
    if not names:
        menu.warn(f"还没有可选择的{label}。")
        return current
    options = [(str(index), name) for index, name in enumerate(names, 1)]
    options.append(("0", "返回"))
    choice = menu.choose(f"选择{label}", options)
    if choice == "0":
        return current
    return names[int(choice) - 1]


def _ask_name(store: ProfileStore, menu, kind: str) -> str:
    label = "机器配置" if kind == "machines" else "作业方案"
    while True:
        try:
            name = menu.ask(f"请输入{label}名称")
            store_path = store._path(kind, name)
            if store_path.exists() and not menu.ask_yes_no(
                    f"{name} 已存在，覆盖吗？", default=False):
                continue
            return name
        except ProfileNameError as exc:
            menu.warn(str(exc))


def run_profile_manager(store: ProfileStore, menu, config_path: Path,
                        geometry=None) -> dict[str, str | None]:
    """Create/select named profiles and persist the current pair."""
    if geometry is None:
        from master.master import _known_geometry
        geometry = _known_geometry()
    selection = store.load_selection()
    local_path = Path(config_path).parent / "config.local.toml"
    while True:
        menu.say("")
        menu.say("=== 配置档案 ===")
        menu.say(f"当前机器配置：{selection['machine'] or '未选择'}")
        menu.say(f"当前作业方案：{selection['job'] or '未选择'}")
        try:
            choice = menu.choose("操作", (
                ("1", "导入当前 config.local.toml 并命名为机器配置"),
                ("2", "新建作业方案"),
                ("3", "选择机器配置"),
                ("4", "选择作业方案"),
                ("0", "返回"),
            ))
        except WizardCancelled:
            return selection

        if choice == "0":
            return selection
        if choice == "1":
            if not local_path.exists():
                menu.warn(f"未找到 {local_path}；先完成一次实机配置向导。")
                continue
            name = _ask_name(store, menu, "machines")
            store.import_legacy_machine(name, local_path)
            selection["machine"] = name
            store.save_selection(**selection)
            menu.say(f"已保存机器配置：{name}")
        elif choice == "2":
            name = _ask_name(store, menu, "jobs")
            defaults = load_merged(Path(config_path)).get("job", {})
            try:
                row, _ = collect_job(menu, geometry, defaults=defaults)
            except WizardCancelled:
                menu.say("已取消新建作业方案。")
                continue
            values = {"job": {**job_values(row), "rows": 1}}
            store.save("jobs", name, values)
            selection["job"] = name
            store.save_selection(**selection)
            menu.say(f"已保存作业方案：{name}")
        elif choice == "3":
            selection["machine"] = _select_existing(
                store, menu, "machines", selection["machine"])
            store.save_selection(**selection)
        elif choice == "4":
            selection["job"] = _select_existing(
                store, menu, "jobs", selection["job"])
            store.save_selection(**selection)

