"""Additive dashboard views for bounded detector-score distributions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import UtcNs
from .dashboard import TimeRangeQuery

SCORE_HISTOGRAM_BIN_COUNT = 40


@dataclass(frozen=True)
class ScoreHistogramBinV0_1:
    index: int
    lower: float
    upper: float
    count: int
    density: float

    def __post_init__(self) -> None:
        if not 0 <= self.index < SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("score histogram index is out of bounds")
        for name in ("lower", "upper", "density"):
            require_finite(getattr(self, name), name)
        if not 0.0 <= self.lower < self.upper <= 1.0:
            raise ValueError("score histogram bin is outside [0,1]")
        if self.count < 0 or self.density < 0.0:
            raise ValueError("score histogram values must be non-negative")


@dataclass(frozen=True)
class MethodScoreDistributionV0_1:
    method: str
    recording_count: int
    score_count: int
    mean: float
    standard_deviation: float
    minimum: float
    maximum: float
    bins: tuple[ScoreHistogramBinV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.method, "method")
        if self.recording_count <= 0 or self.score_count <= 0:
            raise ValueError("score distribution counts must be positive")
        if self.recording_count > self.score_count:
            raise ValueError("recording count exceeds score count")
        for name in ("mean", "standard_deviation", "minimum", "maximum"):
            require_finite(getattr(self, name), name)
        if not 0.0 <= self.minimum <= self.mean <= self.maximum <= 1.0:
            raise ValueError("score distribution summary is outside [0,1]")
        if self.standard_deviation < 0.0:
            raise ValueError("score standard deviation must be non-negative")
        if len(self.bins) != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("score histogram must contain every fixed bin")
        if tuple(item.index for item in self.bins) != tuple(
            range(SCORE_HISTOGRAM_BIN_COUNT)
        ):
            raise ValueError("score histogram bins are not canonical")
        if sum(item.count for item in self.bins) != self.score_count:
            raise ValueError("score histogram count differs from summary")
        width = 1.0 / SCORE_HISTOGRAM_BIN_COUNT
        integral = sum(item.density * width for item in self.bins)
        if abs(integral - 1.0) > 1e-12:
            raise ValueError("score histogram density does not integrate to one")


@dataclass(frozen=True)
class ScoreDistributionViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    score_domain_lower: float
    score_domain_upper: float
    bin_count: int
    semantics: str
    distributions: tuple[MethodScoreDistributionV0_1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported score-distribution schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("score-distribution interval must be non-empty")
        if (self.score_domain_lower, self.score_domain_upper) != (0.0, 1.0):
            raise ValueError("score distribution must expose the native [0,1] domain")
        if self.bin_count != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("score distribution has an unsupported bin count")
        if self.semantics != "candidate-method-score-density":
            raise ValueError("unsupported score-distribution semantics")
        methods = tuple(item.method for item in self.distributions)
        if methods != tuple(sorted(set(methods))):
            raise ValueError("score distributions must have unique sorted methods")


class ScoreDistributionQueryPortV0_1(Protocol):
    def score_distributions(
        self, query: TimeRangeQuery
    ) -> ScoreDistributionViewV0_1: ...


@dataclass(frozen=True)
class PointScoreDistributionV0_2:
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
        for value, name in (
            (self.method, "method"),
            (self.radio_id, "radio_id"),
            (self.receiver_chain_id, "receiver_chain_id"),
        ):
            require_token(value, name)
        if self.edge not in {"lower", "upper"}:
            raise ValueError("unsupported score edge")
        if self.score_kind not in {"candidate", "conditioned-control"}:
            raise ValueError("unsupported score kind")
        if not 0 < self.recording_count <= self.point_count:
            raise ValueError("point distribution counts are invalid")
        for name in ("mean", "standard_deviation", "minimum", "maximum"):
            require_finite(getattr(self, name), name)
        if not 0.0 <= self.minimum <= self.mean <= self.maximum <= 1.0:
            raise ValueError("point score summary is outside [0,1]")
        if self.standard_deviation < 0.0:
            raise ValueError("point score standard deviation must be non-negative")
        if len(self.bins) != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("point score histogram must contain every fixed bin")
        if tuple(item.index for item in self.bins) != tuple(
            range(SCORE_HISTOGRAM_BIN_COUNT)
        ):
            raise ValueError("point score histogram bins are not canonical")
        if sum(item.count for item in self.bins) != self.point_count:
            raise ValueError("point score histogram count differs")


@dataclass(frozen=True)
class PointScoreDistributionViewV0_2:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    score_domain_lower: float
    score_domain_upper: float
    bin_count: int
    point_identity: str
    distributions: tuple[PointScoreDistributionV0_2, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("unsupported point score-distribution schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("point score-distribution interval must be non-empty")
        if (self.score_domain_lower, self.score_domain_upper) != (0.0, 1.0):
            raise ValueError("point score distribution requires native [0,1]")
        if self.bin_count != SCORE_HISTOGRAM_BIN_COUNT:
            raise ValueError("unsupported point score histogram bin count")
        if self.point_identity != (
            "recording+segment+radio+receiver-chain+edge+method"
        ):
            raise ValueError("unsupported point identity")
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
            raise ValueError("point distributions must have unique sorted strata")


class PointScoreDistributionQueryPortV0_2(Protocol):
    def point_score_distributions(
        self, query: TimeRangeQuery
    ) -> PointScoreDistributionViewV0_2: ...
