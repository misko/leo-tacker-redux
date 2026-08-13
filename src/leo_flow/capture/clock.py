"""Explicit clocks keep capture timing deterministic in tests."""

from __future__ import annotations

import time
from typing import Protocol


class CaptureClock(Protocol):
    def now_utc_ns(self) -> int: ...

    def now_monotonic_ns(self) -> int: ...


class SystemCaptureClock:
    def now_utc_ns(self) -> int:
        return time.time_ns()

    def now_monotonic_ns(self) -> int:
        return time.monotonic_ns()
