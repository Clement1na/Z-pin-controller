from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_PAREN_COMMENT = re.compile(r"\([^)]*\)")


class ResponseKind(Enum):
    OK = "ok"
    ERROR = "error"
    ALARM = "alarm"
    STATUS = "status"
    WELCOME = "welcome"
    OTHER = "other"


@dataclass(frozen=True)
class Response:
    kind: ResponseKind
    raw: str
    error_code: int | None = None
    alarm_code: int | None = None
    state: str | None = None


def strip_comments(line: str) -> str:
    if ";" in line:
        line = line.split(";", 1)[0]
    line = _PAREN_COMMENT.sub(" ", line)
    return " ".join(line.split())


def prepare_gcode_line(line: str) -> str | None:
    cleaned = strip_comments(line)
    if not cleaned or cleaned.startswith("%"):
        return None
    return cleaned


def _parse_int_prefix(text: str) -> int | None:
    token = text.strip().split()[0] if text.strip() else ""
    try:
        return int(token)
    except ValueError:
        return None


def parse_response(line: str) -> Response:
    raw = line.strip()
    if raw == "ok":
        return Response(ResponseKind.OK, raw)
    if raw.lower().startswith("error:"):
        code = _parse_int_prefix(raw.split(":", 1)[1])
        return Response(ResponseKind.ERROR, raw, error_code=code)
    if raw.upper().startswith("ALARM:"):
        code = _parse_int_prefix(raw.split(":", 1)[1])
        return Response(ResponseKind.ALARM, raw, alarm_code=code)
    if raw.startswith("<") and raw.endswith(">"):
        state = raw[1:-1].split("|", 1)[0]
        return Response(ResponseKind.STATUS, raw, state=state)
    if raw.startswith("Grbl ") or raw.startswith("FluidNC"):
        return Response(ResponseKind.WELCOME, raw)
    return Response(ResponseKind.OTHER, raw)
