# -*- coding: utf-8 -*-
"""本机配置叠加层。

`master/config.toml` 是随仓库发布的模板，含实测标定值，任何人都不该手改；
`config.local.toml` 与它同级，只存本机环境（串口、网卡、软限位），由向导生成。
读取时后者按配置段叠加在前者之上；本机文件不存在则完全等价于只读模板。

`tomllib` 只能读，而交付物必须零依赖，所以写出器在这里手写。
"""
from __future__ import annotations

import math
import os
import re
import tomllib
from pathlib import Path
from typing import Mapping

LOCAL_NAME = "config.local.toml"

HEADER = (
    "本机配置：由 master/wizard.py 生成，只保存本机环境（串口/网卡/软限位）。",
    "叠加在 config.toml 模板之上，本文件中出现的键优先。",
    "已在 .gitignore 中，请勿提交到版本库。",
)

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def local_path_for(template: Path) -> Path:
    """本机配置与模板同级。"""
    return Path(template).parent / LOCAL_NAME


def load_merged(template: Path, local: Path | None = None) -> dict:
    """模板 + 本机配置合并后的完整配置字典。"""
    template = Path(template)
    base = _read_toml(template)
    overlay_path = local_path_for(template) if local is None else Path(local)
    if not overlay_path.exists():
        return base
    return _merge(base, _read_toml(overlay_path), overlay_path)


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except OSError as exc:
        raise ValueError(f"配置：无法读取 {path}：{exc}") from exc


def _merge(base: dict, overlay: dict, path: Path) -> dict:
    """按配置段一层深合并；本机文件里出现嵌套表即报错。

    只做一层是有意的：`_read_live_configuration` 的 required() 只往下看一层，
    更深的结构它读不到，静默接受只会把错误推迟到运行期。
    """
    merged = {section: (dict(values) if isinstance(values, dict) else values)
              for section, values in base.items()}
    for section, values in overlay.items():
        if not isinstance(values, dict):
            raise ValueError(f"配置：{path} 的 {section} 不是配置段")
        for key, value in values.items():
            if isinstance(value, dict):
                raise ValueError(
                    f"配置：{path} 不支持嵌套表 [{section}.{key}]")
        merged.setdefault(section, {}).update(values)
    return merged


def dumps(values: Mapping[str, Mapping[str, object]]) -> str:
    """最小 TOML 写出器：只支持 [section] 加 str/int/float/bool。"""
    lines: list[str] = []
    for section, entries in values.items():
        if not isinstance(entries, Mapping):
            raise ValueError(f"无法写出非配置段 {section}")
        if lines:
            lines.append("")
        lines.append(f"[{_format_key(section)}]")
        lines.extend(f"{_format_key(key)} = {_format_value(section, key, value)}"
                     for key, value in entries.items())
    return "\n".join(lines) + "\n"


def _format_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _quote(key)


def _quote(text: str) -> str:
    return '"' + "".join(_ESCAPES.get(char, char) for char in text) + '"'


def _format_value(section: str, key: str, value: object) -> str:
    # bool 必须在 int 之前判断：bool 是 int 的子类，否则会写成 1/0，
    # 而预检用的是 `is not True`，读回来就不再相等。
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"[{section}] {key} 不是有限数，无法写出")
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise ValueError(f"[{section}] {key} 的类型 {type(value).__name__} 无法写出")


def write_local(path: Path, values: Mapping[str, Mapping[str, object]]) -> None:
    """原子写出：先写同目录 .tmp，再替换，避免断电留下半截配置。"""
    path = Path(path)
    text = "".join(f"# {line}\n" for line in HEADER) + dumps(values)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
