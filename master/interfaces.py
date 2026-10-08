"""Choose one machine configuration and its interfaces without connecting devices."""
from __future__ import annotations

from pathlib import Path

from master.devices import list_nics, list_serial_ports, rank_nics, rank_serial_ports
from master.local_config import load_merged, local_path_for, write_local


def _select_device(menu, label, *, network, find_adapters=None, list_ports=None):
    while True:
        try:
            devices = (rank_nics(list_nics(find_adapters)) if network
                       else rank_serial_ports(list_serial_ports(list_ports)))
        except Exception as exc:
            menu.warn(f"{label}枚举失败：{exc}；可以手动输入或跳过。")
            devices = []
        options = []
        for index, device in enumerate(devices, 1):
            title = device.description if network else f"{device.device}  {device.description}"
            if device.reject_reason:
                title += f"（{device.reject_reason}）"
            options.append((str(index), title))
        options.extend((("M", "手动输入"), ("R", "重新扫描"), ("S", "跳过此接口")))
        choice = menu.choose(f"选择{label}", options)
        if choice == "S":
            return ""
        if choice == "R":
            continue
        if choice == "M":
            value = menu.ask("网卡描述或设备名" if network else "端口名（例如 COM9）").strip()
            if value:
                return value if network else value.upper()
            menu.warn("接口名称不能为空；未连接时请选择跳过。")
            continue
        device = devices[int(choice) - 1]
        if device.reject_reason:
            menu.warn(f"{label}：{device.reject_reason}。")
            if not menu.ask_yes_no("仍然使用这个接口？", default=False):
                continue
        return device.description if network else device.device.upper()


def _confirm_interface(menu, label, current, *, network=False, excluded=(),
                       find_adapters=None, list_ports=None):
    while True:
        options = [("2", "重新选择实际接口"), ("0", "跳过（未连接）")]
        if current:
            options.insert(0, ("1", f"沿用 {current}"))
        choice = menu.choose(label, options, default="1" if current else "0")
        if choice == "1":
            value = current if network else current.upper()
        elif choice == "2":
            value = _select_device(menu, label, network=network,
                                   find_adapters=find_adapters, list_ports=list_ports)
        else:
            value = ""
        if value and value.casefold() in {item.casefold() for item in excluded if item}:
            menu.warn(f"{value} 已被另一路占用，请另选端口或跳过此接口。")
            continue
        if not value:
            menu.warn(f"已跳过{label}；使用该设备的动作前需要重新选择接口。")
        return value


def prepare_interfaces(config_path, menu, *, store=None, selection=None,
                       find_adapters=None, list_ports=None) -> dict:
    """Return and persist the full chosen configuration, with optional interfaces.

    This step neither opens hardware nor asks for motion limits or calibration.
    The returned snapshot can be passed unchanged into either execution branch.
    """
    config_path = Path(config_path)
    selected = dict(selection if selection is not None else
                    (store.load_selection() if store is not None else {}))
    machine = selected.get("machine")
    names = store.list_names("machines") if store is not None else []
    menu.say("=== 选择用户机器配置与实际接口 ===")
    if names:
        options = [("current", "当前本机配置")]
        options.extend((str(index), name) for index, name in enumerate(names, 1))
        default = str(names.index(machine) + 1) if machine in names else "current"
        choice = menu.choose("本次使用的机器配置", options, default=default)
        machine = None if choice == "current" else names[int(choice) - 1]
    else:
        machine = None
        menu.say("没有命名的机器配置，本次使用当前本机配置。")
    selected["machine"] = machine
    selected.setdefault("custom", None)
    cfg = (store.compose(config_path, machine=machine, custom=selected.get("custom"))
           if store is not None else load_merged(config_path))
    servo, cnc, head = (cfg.setdefault(section, {}) for section in ("servo", "cnc3d", "head"))
    servo["adapter"] = _confirm_interface(
        menu, "伺服 EtherCAT 网卡", servo.get("adapter", ""), network=True,
        find_adapters=find_adapters)
    cnc["port"] = _confirm_interface(menu, "CNC 串口", cnc.get("port", ""), list_ports=list_ports)
    head["port"] = _confirm_interface(menu, "GX28 滑台串口", head.get("port", ""),
                                      excluded=(cnc["port"],), list_ports=list_ports)
    write_local(local_path_for(config_path), cfg)
    if store is not None:
        if machine:
            store.save("machines", machine, {key: values for key, values in cfg.items() if key != "custom"})
        store.save_selection(machine=machine, custom=selected.get("custom"))
    if selection is not None:
        selection.update(machine=machine, custom=selected.get("custom"))
    menu.say("接口选择已保存；尚未连接或移动任何设备。")
    return cfg
