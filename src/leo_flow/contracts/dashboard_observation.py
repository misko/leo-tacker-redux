"""Additive dashboard aggregates for captured RF exposure and Starlink evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_token, require_utc_ns
from .core import RecordingId, UtcNs
from .dashboard import TimeRangeQuery


@dataclass(frozen=True)
class DutyCycleAggregateV0_1:
    dimension: str
    identity: str
    active_ns: int
    interval_ns: int
    duty_cycle: float

    def __post_init__(self) -> None:
        if self.dimension not in {"radio", "lnb"}:
            raise ValueError("unsupported duty-cycle dimension")
        require_token(self.identity, "identity")
        if self.interval_ns <= 0 or not 0 <= self.active_ns <= self.interval_ns:
            raise ValueError("invalid duty-cycle interval")
        if not 0.0 <= self.duty_cycle <= 1.0:
            raise ValueError("duty_cycle must be bounded")


@dataclass(frozen=True)
class StarlinkEvidenceAggregateV0_1:
    dimension: str
    identity: str
    comparison_count: int
    candidate_positive_count: int
    candidate_positive_rate: float | None
    calibrated_detection_count: int | None
    calibrated_detection_rate: float | None

    def __post_init__(self) -> None:
        if self.dimension not in {"lnb", "edge", "method"}:
            raise ValueError("unsupported Starlink aggregate dimension")
        require_token(self.identity, "identity")
        if not 0 <= self.candidate_positive_count <= self.comparison_count:
            raise ValueError("invalid Starlink comparison counts")
        expected = (
            None
            if self.comparison_count == 0
            else self.candidate_positive_count / self.comparison_count
        )
        if self.candidate_positive_rate != expected:
            raise ValueError("candidate-positive rate differs from counts")
        if (
            self.calibrated_detection_count is not None
            or self.calibrated_detection_rate is not None
        ):
            raise ValueError("v0.1 aggregate is candidate-only")


@dataclass(frozen=True)
class RecordingStarlinkStateV0_1:
    recording_id: RecordingId
    state: str

    def __post_init__(self) -> None:
        if self.state not in {"candidates", "not_evaluated", "unavailable"}:
            raise ValueError("unsupported recording Starlink state")


@dataclass(frozen=True)
class ObservationAggregateViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    recording_count: int
    candidate_recording_count: int
    not_evaluated_recording_count: int
    unavailable_recording_count: int
    calibration_status: str
    calibration_reason: str
    duty_cycles: tuple[DutyCycleAggregateV0_1, ...]
    starlink_evidence: tuple[StarlinkEvidenceAggregateV0_1, ...]
    recording_states: tuple[RecordingStarlinkStateV0_1, ...]
    recording_states_truncated: bool

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported observation aggregate schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("aggregate interval must be non-empty")
        counts = (
            self.candidate_recording_count
            + self.not_evaluated_recording_count
            + self.unavailable_recording_count
        )
        if self.recording_count < 0 or counts != self.recording_count:
            raise ValueError("recording aggregate counts differ")
        if self.calibration_status != "required":
            raise ValueError("v0.1 aggregate requires calibration")
        require_token(self.calibration_reason, "calibration_reason")
        if len(self.recording_states) > 10_000:
            raise ValueError("recording state disclosure is unbounded")


class ObservationAggregateQueryPortV0_1(Protocol):
    def observation_aggregate(
        self, query: TimeRangeQuery
    ) -> ObservationAggregateViewV0_1: ...
