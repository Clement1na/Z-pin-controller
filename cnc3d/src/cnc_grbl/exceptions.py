class GrblException(Exception):
    """Base error for the Grbl serial client."""


class GrblError(GrblException):
    """The controller replied with ``error:N``."""

    def __init__(self, code: int | None, raw: str = "") -> None:
        self.code = code
        self.raw = raw
        super().__init__(raw or f"error:{code}")


class GrblAlarm(GrblException):
    """The controller reported ``ALARM:N``."""

    def __init__(self, code: int | None, raw: str = "") -> None:
        self.code = code
        self.raw = raw
        super().__init__(raw or f"ALARM:{code}")


class GrblTimeout(GrblException):
    """Timed out waiting for a Grbl response."""
