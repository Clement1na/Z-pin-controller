"""User-facing interface choices and shared persistence; never opens hardware."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re

from master.devices import list_nics, list_serial_ports, rank_nics, rank_serial_ports
from master.local_config import local_path_for, write_local
from master.profiles import validate_profile_name


@dataclass(frozen=True)
class InterfaceCatalog:
    nics: tuple
    ports: tuple
    hidden_nics: int
    hidden_ports: int
    errors: tuple[str, ...]


def scan_interfaces(*, find_adapters=None, list_ports=None):
    errors = []
    try:
        all_nics = rank_nics(list_nics(find_adapters))
    except Exception as exc:
        errors.append(f"EtherCAT网卡扫描失败：{exc}")
        all_nics = []
    try:
        all_ports = rank_serial_ports(list_serial_ports(list_ports))
    except Exception as exc:
        errors.append(f"USB串口扫描失败：{exc}")
        all_ports = []
    nics = tuple(n for n in all_nics if not n.reject_reason
                 and not any(word in n.description.lower() for word in ('无线', '蓝牙')))
    ports = tuple(p for p in all_ports if not p.reject_reason and p.is_usb
                  and not any(word in p.description.lower() for word in ('bluetooth', '蓝牙')))
    return InterfaceCatalog(nics, ports, len(all_nics)-len(nics), len(all_ports)-len(ports), tuple(errors))


def ranked_candidates(catalog, component, current):
    values = catalog.nics if component == 'servo' else catalog.ports
    def saved(device):
        identifiers = ((device.name, device.description) if component == 'servo' else (device.device,))
        return bool(current) and any(str(current).casefold() == value.casefold() for value in identifiers)
    return sorted(values, key=lambda device: not saved(device))


def save_interface_selection(config_path, store, selection, *, cfg, machine, save_as,
                             adapter, cnc_port, head_port):
    result = deepcopy(cfg)
    adapter = adapter.strip()
    cnc_port, head_port = cnc_port.strip().upper(), head_port.strip().upper()
    for port in (cnc_port, head_port):
        if port and not re.fullmatch(r'COM[1-9]\d*', port):
            raise ValueError("串口请输入COM编号，例如COM9；未连接时留空")
    if cnc_port and cnc_port == head_port:
        raise ValueError("CNC和GX28不能占用同一个串口")
    target = save_as.strip() or machine
    if target:
        validate_profile_name(target)
        if store is not None and target != machine and target in store.list_names('machines'):
            raise ValueError(f'机器配置“{target}”已存在，请先选择该配置，或使用新名称')
    result.setdefault('servo', {})['adapter'] = adapter
    result.setdefault('cnc3d', {})['port'] = cnc_port
    result.setdefault('head', {})['port'] = head_port
    if target and store is not None:
        store.save('machines', target, {k: v for k, v in result.items() if k != 'custom'})
    write_local(local_path_for(Path(config_path)), result)
    if store is not None:
        store.save_selection(machine=target, custom=selection.get('custom'))
    selection['machine'] = target
    return result


def prepare_user_interfaces(config_path, menu, *, store, selection):
    catalog = scan_interfaces()
    for error in catalog.errors:
        menu.warn(error)
    return menu.configure_interfaces(Path(config_path), store, selection, catalog)
