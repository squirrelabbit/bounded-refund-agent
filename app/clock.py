"""Injectable clock. Scenarios use a fixed clock so every run is reproducible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Starts at ``base`` and advances ``step_seconds`` on every reading.

    With the default step of zero the clock never moves, which is what most
    scenarios want. Scenarios that need a permit to expire either set a step or
    call :meth:`advance` explicitly.
    """

    def __init__(self, base: str | datetime, step_seconds: float = 0.0) -> None:
        self._current = parse_iso(base) if isinstance(base, str) else base
        if self._current.tzinfo is None:
            self._current = self._current.replace(tzinfo=UTC)
        self._step = timedelta(seconds=step_seconds)
        self.readings = 0

    def now(self) -> datetime:
        current = self._current
        self._current = current + self._step
        self.readings += 1
        return current

    def peek(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current = self._current + timedelta(seconds=seconds)
