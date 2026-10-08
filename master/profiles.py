"""Named machine/custom profile storage for the interactive CLI."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Mapping

from master.local_config import dumps, load_merged


_PROFILE_DIRS = {"machines": "machines", "custom": "custom"}
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ProfileNameError(ValueError):
    """A profile name cannot be represented safely as a Windows filename."""


def validate_profile_name(name: str) -> str:
    if not isinstance(name, str):
        raise ProfileNameError("配置名称必须是文字")
    if not name or name in {".", ".."}:
        raise ProfileNameError("配置名称不能为空或点目录")
    if name != name.strip() or name.endswith((".", " ")):
        raise ProfileNameError("配置名称不能以空格或句点结尾")
    if _INVALID_NAME.search(name):
        raise ProfileNameError("配置名称含有 Windows 文件名不允许的字符")
    if name.upper() in _RESERVED_NAMES:
        raise ProfileNameError("配置名称不能使用 Windows 保留设备名")
    return name


def _read_toml(path: Path) -> dict:
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


def _validate_kind(kind: str) -> str:
    if kind not in _PROFILE_DIRS:
        raise ValueError(f"未知配置类型：{kind}")
    return kind


def _validate_sections(values: Mapping[str, Mapping[str, object]]) -> dict:
    if not isinstance(values, Mapping):
        raise ValueError("配置内容必须是按分组组织的表")
    result: dict[str, dict[str, object]] = {}
    for section, entries in values.items():
        if not isinstance(section, str) or not section:
            raise ValueError("配置分组名称必须是非空文字")
        if not isinstance(entries, Mapping):
            raise ValueError(f"配置分组 [{section}] 必须是表")
        copied: dict[str, object] = {}
        for key, value in entries.items():
            if isinstance(value, Mapping):
                raise ValueError(f"配置 [{section}] 不支持嵌套分组")
            copied[key] = value
        result[section] = copied
    return result


def _overlay(base: Mapping[str, Mapping[str, object]],
             extra: Mapping[str, Mapping[str, object]]) -> dict:
    merged = {section: dict(entries) for section, entries in base.items()}
    for section, entries in _validate_sections(extra).items():
        merged.setdefault(section, {}).update(entries)
    return merged


class ProfileStore:
    """Persist named machine/custom TOML files beside the executable."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.machine_dir = self.root / "machines"
        self.selection_path = self.root / "selection.toml"

    def _path(self, kind: str, name: str) -> Path:
        _validate_kind(kind)
        validate_profile_name(name)
        return self.root / _PROFILE_DIRS[kind] / f"{name}.toml"

    def list_names(self, kind: str) -> list[str]:
        _validate_kind(kind)
        folder = self.root / _PROFILE_DIRS[kind]
        if not folder.exists():
            return []
        return sorted(path.stem for path in folder.glob("*.toml")
                      if path.is_file() and path.stem not in {".", ".."})

    def load(self, kind: str, name: str) -> dict:
        path = self._path(kind, name)
        if not path.exists():
            raise FileNotFoundError(f"配置不存在：{kind}/{name}")
        return _validate_sections(_read_toml(path))

    def save(self, kind: str, name: str, values) -> Path:
        path = self._path(kind, name)
        content = dumps(_validate_sections(values))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        return path

    def delete(self, kind: str, name: str, *, selected: bool = False) -> None:
        path = self._path(kind, name)
        if selected:
            raise ValueError("不能删除当前正在使用的配置")
        path.unlink()

    def import_legacy_machine(self, name: str, local_path: Path) -> Path:
        values = _validate_sections(_read_toml(Path(local_path)))
        return self.save("machines", name, values)

    def load_selection(self) -> dict[str, str | None]:
        if not self.selection_path.exists():
            return {"machine": None, "custom": None}
        values = _read_toml(self.selection_path).get("selection", {})
        if not isinstance(values, Mapping):
            raise ValueError("selection.toml 的 [selection] 必须是表")
        machine = values.get("machine") or None
        custom = values.get("custom") or None
        if machine is not None:
            validate_profile_name(machine)
        if custom is not None:
            validate_profile_name(custom)
        return {"machine": machine, "custom": custom}

    def save_selection(self, *, machine: str | None, custom: str | None) -> None:
        if machine is not None:
            validate_profile_name(machine)
        if custom is not None:
            validate_profile_name(custom)
        self.root.mkdir(parents=True, exist_ok=True)
        content = dumps({"selection": {
            "machine": machine or "",
            "custom": custom or "",
        }})
        tmp = self.selection_path.with_name(self.selection_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.selection_path)

    def compose(self, template: Path, *, machine: str | None = None,
                custom: str | None = None) -> dict:
        base = load_merged(Path(template))
        if machine:
            base = _overlay(base, self.load("machines", machine))
        if custom:
            base = _overlay(base, self.load("custom", custom))
        return base
