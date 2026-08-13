"""Explicit fake UTC/monotonic clock; scientific code should never read global time."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeClock:
    utc_ns: int = 1_700_000_000_000_000_000
    monotonic_ns: int = 1_000_000_000

    def now_utc_ns(self) -> int:
        return self.utc_ns

    def now_monotonic_ns(self) -> int:
        return self.monotonic_ns

    def advance_ns(self, duration_ns: int) -> None:
        if duration_ns < 0:
            raise ValueError("fake clock cannot move backwards")
        self.utc_ns += duration_ns
        self.monotonic_ns += duration_ns
