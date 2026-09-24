from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cnc_grbl.exceptions import GrblAlarm, GrblError, GrblException, GrblTimeout
from cnc_grbl.protocol import Response, ResponseKind, parse_response, prepare_gcode_line

SOFT_RESET = bytes([0x18])


class GrblController:
    """Send G-code to a Grbl / FluidNC board over a serial port."""

    def __init__(
        self,
        port: str = "COM9",
        baudrate: int = 115200,
        timeout: float = 10.0,
        serial_port: Any | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.welcome: str | None = None
        self._injected_serial = serial_port
        self._serial: Any | None = None
        self._incoming: queue.Queue[Response] = queue.Queue()
        self._running = False
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._last_status: Response | None = None

    def connect(self) -> str:
        if self._serial is None:
            self._serial = self._open_serial()
        if not self._running:
            self._running = True
            self._reader = threading.Thread(
                target=self._reader_loop,
                name="grbl-rx",
                daemon=True,
            )
            self._reader.start()
        self.welcome = self.reset()
        return self.welcome

    def close(self) -> None:
        self._running = False
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        self._serial = None

    def __enter__(self) -> GrblController:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def send(self, line: str, timeout: float | None = None) -> list[str]:
        prepared = prepare_gcode_line(line)
        if prepared is None:
            return []
        self._require_serial()
        self._write(f"{prepared}\n".encode("ascii", errors="strict"))
        return self._wait_ok(timeout if timeout is not None else self.timeout)

    def send_lines(self, lines: Iterable[str], timeout: float | None = None) -> list[str]:
        extras: list[str] = []
        for line in lines:
            extras.extend(self.send(line, timeout=timeout))
        return extras

    def send_file(self, path: str | Path, timeout: float | None = None) -> list[str]:
        text = Path(path).read_text(encoding="utf-8")
        return self.send_lines(text.splitlines(), timeout=timeout)

    def reset(self) -> str:
        self._require_serial()
        self._drain()
        self._write_realtime(SOFT_RESET)
        welcome = self._wait_for(ResponseKind.WELCOME, self.timeout, "reset welcome")
        self.welcome = welcome.raw
        return welcome.raw

    def hold(self) -> None:
        self._write_realtime(b"!")

    def resume(self) -> None:
        self._write_realtime(b"~")

    def status(self, timeout: float = 2.0) -> Response:
        self._require_serial()
        self._write_realtime(b"?")
        report = self._wait_for(ResponseKind.STATUS, timeout, "status")
        self._last_status = report
        return report

    def unlock(self) -> list[str]:
        return self.send("$X")

    def home(self, timeout: float = 60.0) -> list[str]:
        return self.send("$H", timeout=timeout)

    def wait_idle(self, timeout: float = 30.0, poll_interval: float = 0.2) -> Response:
        deadline = time.monotonic() + timeout
        last: Response | None = None
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            last = self.status(timeout=min(2.0, remaining))
            state = (last.state or "").split(":", 1)[0]
            if state.lower() == "idle":
                return last
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        raise GrblTimeout("timed out waiting for Idle")

    def _open_serial(self) -> Any:
        if self._injected_serial is not None:
            return self._injected_serial
        import serial

        return serial.Serial(self.port, self.baudrate, timeout=0.1)

    def _require_serial(self) -> Any:
        if self._serial is None:
            raise GrblException("serial port is not connected")
        return self._serial

    def _write(self, data: bytes) -> None:
        serial_port = self._require_serial()
        with self._write_lock:
            serial_port.write(data)

    def _write_realtime(self, data: bytes) -> None:
        self._write(data)

    def _reader_loop(self) -> None:
        while self._running:
            serial_port = self._serial
            if serial_port is None:
                break
            try:
                raw = serial_port.readline()
            except Exception:
                if not self._running:
                    break
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            response = parse_response(text)
            if response.kind == ResponseKind.STATUS:
                self._last_status = response
            self._incoming.put(response)

    def _drain(self) -> None:
        while True:
            try:
                self._incoming.get_nowait()
            except queue.Empty:
                return

    def _wait_ok(self, timeout: float) -> list[str]:
        extras: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GrblTimeout("timed out waiting for ok")
            response = self._get_response(remaining)
            if response.kind == ResponseKind.STATUS:
                continue
            if response.kind == ResponseKind.OK:
                return extras
            if response.kind == ResponseKind.ERROR:
                raise GrblError(response.error_code, response.raw)
            if response.kind == ResponseKind.ALARM:
                raise GrblAlarm(response.alarm_code, response.raw)
            extras.append(response.raw)

    def _wait_for(self, kind: ResponseKind, timeout: float, what: str) -> Response:
        extras: list[Response] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for item in extras:
                    self._incoming.put(item)
                raise GrblTimeout(f"timed out waiting for {what}")
            response = self._get_response(remaining)
            if response.kind == kind:
                for item in extras:
                    self._incoming.put(item)
                return response
            extras.append(response)

    def _get_response(self, timeout: float) -> Response:
        try:
            return self._incoming.get(timeout=timeout)
        except queue.Empty:
            raise GrblTimeout("timed out waiting for serial response") from None
