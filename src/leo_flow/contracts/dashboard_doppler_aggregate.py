"""Bounded aggregate read model for candidate-only Doppler evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import UtcNs

MAX_DOPPLER_AGGREGATE_SERIES = 512
MAX_DOPPLER_AGGREGATE_POINTS = 8_192
MAX_DOPPLER_AGGREGATE_CONTROL_POINTS = 8_192


@dataclass(frozen=True)
class DopplerAggregateQueryV0_1:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    methods: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    radio_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()
    association_states: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("Doppler aggregate interval must be non-empty")
        allowlists = {
            "methods": {"basic", "advanced"},
            "models": {"constant", "linear", "quadratic", "slope-bank"},
            "edges": {"lower", "upper", "unknown"},
            "association_states": {
                "basic-candidate",
                "matched-basic-candidate",
                "advanced-path-only",
            },
        }
        for name in (
            "methods",
            "models",
            "radio_ids",
            "receiver_chain_ids",
            "channels",
            "edges",
            "association_states",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be unique and sorted")
            if name in allowlists and not set(values) <= allowlists[name]:
                raise ValueError(f"{name} contains an unsupported value")
            for value in values:
                require_token(value, name)


@dataclass(frozen=True)
class DopplerAggregateTrackPointV0_1:
    midpoint_utc_ns: UtcNs
    relative_time_s: float
    frequency_offset_hz: float

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        require_finite(self.relative_time_s, "relative_time_s")
        require_finite(self.frequency_offset_hz, "frequency_offset_hz")


@dataclass(frozen=True)
class DopplerAggregateSeriesV0_1:
    recording_id: str
    recording_started_utc_ns: UtcNs
    radio_id: str
    receiver_chain_id: str
    segment_id: str
    channel: str
    edge: str
    doppler_id: str
    waterfall_product_id: str
    candidate_or_path_id: str
    method: str
    algorithm_version: str
    model: str
    association_state: str
    reference_utc_ns: UtcNs
    reference_frequency_hz: float | None
    drift_rate_hz_s: float
    ranking_or_heldout_score: float
    input_identity_digest: str
    config_digest: str
    basic_bundle_digest: str
    advanced_bundle_digest: str
    overlapping_observations: bool
    points: tuple[DopplerAggregateTrackPointV0_1, ...]

    def __post_init__(self) -> None:
        for name in (
            "recording_id",
            "radio_id",
            "receiver_chain_id",
            "segment_id",
            "channel",
            "doppler_id",
            "waterfall_product_id",
            "candidate_or_path_id",
            "algorithm_version",
            "input_identity_digest",
            "config_digest",
            "basic_bundle_digest",
            "advanced_bundle_digest",
        ):
            require_token(getattr(self, name), name)
        require_utc_ns(self.recording_started_utc_ns, "recording_started_utc_ns")
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        if self.method not in {"basic", "advanced"}:
            raise ValueError("unsupported Doppler aggregate method")
        if self.model not in {"constant", "linear", "quadratic", "slope-bank"}:
            raise ValueError("unsupported Doppler aggregate model")
        if self.edge not in {"lower", "upper", "unknown"}:
            raise ValueError("unsupported Doppler aggregate edge")
        if self.association_state not in {
            "basic-candidate",
            "matched-basic-candidate",
            "advanced-path-only",
        }:
            raise ValueError("unsupported candidate association state")
        for name in ("drift_rate_hz_s", "ranking_or_heldout_score"):
            require_finite(getattr(self, name), name)
        if self.reference_frequency_hz is not None:
            require_finite(self.reference_frequency_hz, "reference_frequency_hz")
        if self.method == "basic" and len(self.points) < 2:
            raise ValueError("basic Doppler series requires physical track points")
        if self.method == "advanced" and self.points:
            raise ValueError("advanced slope-bank bins are not physical frequencies")


@dataclass(frozen=True)
class DopplerAggregateControlPointV0_1:
    recording_id: str
    radio_id: str
    receiver_chain_id: str
    segment_id: str
    candidate_path_id: str
    control_class: str
    score: float

    def __post_init__(self) -> None:
        for name in (
            "recording_id",
            "radio_id",
            "receiver_chain_id",
            "segment_id",
            "candidate_path_id",
        ):
            require_token(getattr(self, name), name)
        if self.control_class not in {
            "heldout-path",
            "stationary",
            "opposite-slope",
            "time-shuffle",
        }:
            raise ValueError("unsupported Doppler control class")
        require_finite(self.score, "score")


@dataclass(frozen=True)
class DopplerAggregateSummaryV0_1:
    radio_id: str
    receiver_chain_id: str
    method: str
    model: str
    association_state: str
    series_count: int
    median_drift_rate_hz_s: float
    p10_drift_rate_hz_s: float
    p90_drift_rate_hz_s: float

    def __post_init__(self) -> None:
        for name in ("radio_id", "receiver_chain_id"):
            require_token(getattr(self, name), name)
        if self.series_count < 1:
            raise ValueError("Doppler aggregate summary is empty")
        for name in (
            "median_drift_rate_hz_s",
            "p10_drift_rate_hz_s",
            "p90_drift_rate_hz_s",
        ):
            require_finite(getattr(self, name), name)
        if (
            not self.p10_drift_rate_hz_s
            <= self.median_drift_rate_hz_s
            <= self.p90_drift_rate_hz_s
        ):
            raise ValueError("Doppler aggregate quantiles are unordered")


@dataclass(frozen=True)
class DopplerAggregateViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    recording_count: int
    tile_count: int
    available_recording_count: int
    truncated: bool
    series: tuple[DopplerAggregateSeriesV0_1, ...]
    controls: tuple[DopplerAggregateControlPointV0_1, ...]
    summaries: tuple[DopplerAggregateSummaryV0_1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported Doppler aggregate schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("Doppler aggregate interval must be non-empty")
        for name in ("recording_count", "tile_count", "available_recording_count"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} is negative")
        if len(self.series) > MAX_DOPPLER_AGGREGATE_SERIES:
            raise ValueError("too many Doppler aggregate series")
        if sum(len(item.points) for item in self.series) > MAX_DOPPLER_AGGREGATE_POINTS:
            raise ValueError("too many Doppler aggregate track points")
        if len(self.controls) > MAX_DOPPLER_AGGREGATE_CONTROL_POINTS:
            raise ValueError("too many Doppler aggregate control points")
        required = {
            "candidate-only-evidence-not-satellite-detection",
            "overlapping-track-observations-are-not-independent",
            "advanced-path-bins-not-converted-to-physical-frequency",
        }
        if not required <= set(self.warnings):
            raise ValueError("Doppler aggregate lacks safety warnings")


class DopplerAggregateQueryPortV0_1(Protocol):
    def doppler_aggregate(
        self, query: DopplerAggregateQueryV0_1
    ) -> DopplerAggregateViewV0_1: ...
