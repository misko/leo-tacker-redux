"""Bounded aggregate summaries of stratified temporal pilot evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import UtcNs
from .dashboard import TimeRangeQuery


@dataclass(frozen=True)
class TemporalPilotAggregateStratumV0_1:
    method: str
    radio_id: str
    receiver_chain_id: str
    edge: str
    recording_count: int
    probe_count: int
    mean_probe_maximum_qin_score: float
    mean_probe_maximum_surrogate_score: float
    mean_union_coverage_fraction: float
    candidate_window_fraction: float

    def __post_init__(self) -> None:
        for value, label in (
            (self.method, "method"),
            (self.radio_id, "radio_id"),
            (self.receiver_chain_id, "receiver_chain_id"),
        ):
            require_token(value, label)
        if self.edge not in {"lower", "upper"}:
            raise ValueError("invalid temporal aggregate edge")
        if not 0 < self.recording_count <= self.probe_count:
            raise ValueError("invalid temporal aggregate counts")
        for label in (
            "mean_probe_maximum_qin_score",
            "mean_probe_maximum_surrogate_score",
            "mean_union_coverage_fraction",
            "candidate_window_fraction",
        ):
            value = getattr(self, label)
            require_finite(value, label)
            if not 0 <= value <= 1:
                raise ValueError(f"{label} must lie in [0,1]")


@dataclass(frozen=True)
class TemporalPilotAggregateViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    recording_count: int
    truncated: bool
    strata: tuple[TemporalPilotAggregateStratumV0_1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported temporal aggregate schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns or self.recording_count < 0:
            raise ValueError("invalid temporal aggregate interval/count")
        required = {
            "stratified-sampling-not-continuous-coverage",
            "candidate-evidence-not-calibrated-detection",
        }
        if not required <= set(self.warnings):
            raise ValueError("temporal aggregate lacks safety disclosures")


class TemporalPilotAggregateQueryPortV0_1(Protocol):
    def temporal_pilot_aggregate(
        self, query: TimeRangeQuery
    ) -> TemporalPilotAggregateViewV0_1: ...
