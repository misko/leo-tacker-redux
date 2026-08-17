"""Bounded dashboard query contract for digital-twin comparison summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_token, require_utc_ns
from .core import UtcNs
from .digital_twin import (
    DigitalTwinComparisonViewV0_1,
    DigitalTwinStatisticKind,
)

MAX_DASHBOARD_TWIN_METHOD_FILTERS = 32
MAX_DASHBOARD_TWIN_JSON_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class DigitalTwinComparisonDashboardQueryV0_1:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    method_ids: tuple[str, ...] = ()
    statistics: tuple[DigitalTwinStatisticKind, ...] = ()

    def __post_init__(self) -> None:
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("digital-twin comparison interval must be non-empty")
        if not len(self.method_ids) <= MAX_DASHBOARD_TWIN_METHOD_FILTERS:
            raise ValueError("digital-twin method filter count exceeds its bound")
        if self.method_ids != tuple(sorted(set(self.method_ids))):
            raise ValueError("digital-twin method filters must be unique and sorted")
        for method_id in self.method_ids:
            require_token(method_id, "method_id")
        expected_statistics = tuple(
            sorted(set(self.statistics), key=lambda item: item.value)
        )
        if self.statistics != expected_statistics:
            raise ValueError("digital-twin statistic filters must be unique and sorted")


class DigitalTwinComparisonQueryPortV0_1(Protocol):
    """Read only dashboard DTOs; implementations must not expose twin truth."""

    def digital_twin_comparison(
        self, query: DigitalTwinComparisonDashboardQueryV0_1
    ) -> DigitalTwinComparisonViewV0_1: ...
