"""Dashboard contract for bounded Qin-versus-surrogate score distributions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import UtcNs
from .dashboard import TimeRangeQuery
from .dashboard_score_distribution import (
    SCORE_HISTOGRAM_BIN_COUNT,
    ScoreHistogramBinV0_1,
)


@dataclass(frozen=True)
class SurrogateScoreDistributionV0_1:
    method: str
    radio_id: str
    receiver_chain_id: str
    edge: str
    score_kind: str
    recording_count: int
    point_count: int
    mean: float
    standard_deviation: float
    minimum: float
    maximum: float
    bins: tuple[ScoreHistogramBinV0_1, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.method, "method"),
            (self.radio_id, "radio_id"),
            (self.receiver_chain_id, "receiver_chain_id"),
        ):
            require_token(value, label)
        if self.edge not in {"lower", "upper"}:
            raise ValueError("unsupported surrogate-distribution edge")
        if self.score_kind not in {"qin", "surrogate"}:
            raise ValueError("unsupported surrogate-distribution score kind")
        if not 0 < self.recording_count <= self.point_count:
            raise ValueError("surrogate-distribution counts are invalid")
        for label in ("mean", "standard_deviation", "minimum", "maximum"):
            require_finite(getattr(self, label), label)
        if not 0.0 <= self.minimum <= self.mean <= self.maximum <= 1.0:
            raise ValueError("surrogate-distribution summary is outside [0,1]")
        if self.standard_deviation < 0:
            raise ValueError("surrogate-distribution deviation is negative")
        if len(self.bins) != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("surrogate distribution must contain every bin")
        if sum(item.count for item in self.bins) != self.point_count:
            raise ValueError("surrogate histogram count differs from summary")


@dataclass(frozen=True)
class SurrogateScoreDistributionViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    bin_count: int
    point_identity: str
    recording_count: int
    truncated: bool
    distributions: tuple[SurrogateScoreDistributionV0_1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported surrogate-distribution schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("surrogate-distribution interval must be non-empty")
        if self.bin_count != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("unsupported surrogate histogram bin count")
        if self.point_identity != (
            "recording+segment+radio+receiver-chain+edge+method+pattern"
        ):
            raise ValueError("unsupported surrogate-distribution point identity")
        if self.recording_count < 0:
            raise ValueError("recording count is negative")
        required = {
            "finite-surrogate-ensemble-not-calibrated-null-distribution",
            "candidate-evidence-not-detection",
        }
        if not required <= set(self.warnings):
            raise ValueError("surrogate distribution lacks safety warnings")
        keys = tuple(
            (
                item.method,
                item.radio_id,
                item.receiver_chain_id,
                item.edge,
                item.score_kind,
            )
            for item in self.distributions
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("surrogate distributions are not canonical")


class SurrogateScoreDistributionQueryPortV0_1(Protocol):
    def surrogate_score_distributions(
        self, query: TimeRangeQuery
    ) -> SurrogateScoreDistributionViewV0_1: ...
