"""Bounded candidate-only Doppler summaries for the master capture table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import RadioId, ReceiverChainId, RecordingId, SegmentId, UtcNs

MAX_CAPTURE_DOPPLER_RECORDINGS = 400
MAX_CAPTURE_DOPPLER_RECEIVERS_PER_RECORDING = 16


class CaptureDopplerState(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class CaptureDopplerSummaryQueryV0_1:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    maximum_recordings: int = MAX_CAPTURE_DOPPLER_RECORDINGS

    def __post_init__(self) -> None:
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("capture Doppler interval must be non-empty")
        if not 1 <= self.maximum_recordings <= MAX_CAPTURE_DOPPLER_RECORDINGS:
            raise ValueError("capture Doppler recording bound is invalid")


@dataclass(frozen=True)
class CaptureDopplerHardwareAssignmentV0_1:
    receiver_chain_id: ReceiverChainId
    lnb_id: str

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")


@dataclass(frozen=True)
class CaptureDopplerScopeRecordingV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    analysis_state: str
    assignments: tuple[CaptureDopplerHardwareAssignmentV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.analysis_state, "analysis_state")
        receivers = tuple(item.receiver_chain_id for item in self.assignments)
        if len(receivers) != len(set(receivers)):
            raise ValueError("capture Doppler assignments must be unique")
        if len(receivers) > MAX_CAPTURE_DOPPLER_RECEIVERS_PER_RECORDING:
            raise ValueError("capture Doppler assignment count is out of bounds")


@dataclass(frozen=True)
class CaptureDopplerScopeViewV0_1:
    recordings: tuple[CaptureDopplerScopeRecordingV0_1, ...]
    original_recording_count: int
    truncated: bool

    def __post_init__(self) -> None:
        identities = tuple(item.recording_id for item in self.recordings)
        if len(identities) != len(set(identities)):
            raise ValueError("capture Doppler scope recordings must be unique")
        if self.original_recording_count < len(self.recordings):
            raise ValueError("capture Doppler scope count is inconsistent")
        if self.truncated != (self.original_recording_count > len(self.recordings)):
            raise ValueError("capture Doppler scope truncation is inconsistent")


class CaptureDopplerScopeQueryPortV0_1(Protocol):
    def capture_doppler_scope(
        self, query: CaptureDopplerSummaryQueryV0_1
    ) -> CaptureDopplerScopeViewV0_1: ...


@dataclass(frozen=True)
class CaptureDopplerCandidateSummaryV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    candidate_id: str
    model: str
    drift_rate_hz_s: float
    ranking_score: float
    doppler_id: str
    algorithm_version: str
    selection: str = "highest-public-ranking-score-per-recording-radio-lnb-receiver"

    def __post_init__(self) -> None:
        for name in (
            "lnb_id",
            "candidate_id",
            "model",
            "doppler_id",
            "algorithm_version",
        ):
            require_token(getattr(self, name), name)
        require_finite(self.drift_rate_hz_s, "drift_rate_hz_s")
        require_finite(self.ranking_score, "ranking_score")
        if (
            self.selection
            != "highest-public-ranking-score-per-recording-radio-lnb-receiver"
        ):
            raise ValueError("unsupported capture Doppler candidate selection")


@dataclass(frozen=True)
class CaptureDopplerRecordingSummaryV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    analysis_state: str
    state: CaptureDopplerState
    candidates: tuple[CaptureDopplerCandidateSummaryV0_1, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        require_token(self.analysis_state, "analysis_state")
        if not isinstance(self.state, CaptureDopplerState):
            raise TypeError("state must be CaptureDopplerState")
        if self.state is CaptureDopplerState.COMPLETE and not self.candidates:
            raise ValueError("complete capture Doppler summary requires candidates")
        if self.state is not CaptureDopplerState.COMPLETE and self.candidates:
            raise ValueError(
                "incomplete capture Doppler summary cannot expose candidates"
            )
        if len(self.candidates) > MAX_CAPTURE_DOPPLER_RECEIVERS_PER_RECORDING:
            raise ValueError("capture Doppler candidate count is out of bounds")
        identities = tuple(
            (item.lnb_id, item.receiver_chain_id) for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError(
                "capture Doppler candidates must not pool receiver identity"
            )
        if any(
            item.recording_id != self.recording_id or item.radio_id != self.radio_id
            for item in self.candidates
        ):
            raise ValueError("capture Doppler candidate belongs to another row")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("capture Doppler reasons must be unique and canonical")
        for value in self.reason_codes:
            require_token(value, "reason_code")


@dataclass(frozen=True)
class CaptureDopplerSummaryViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    candidate_only: bool
    calibrated_detection_count: None
    recordings: tuple[CaptureDopplerRecordingSummaryV0_1, ...]
    original_recording_count: int
    truncated: bool
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported capture Doppler summary schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("capture Doppler summary interval must be non-empty")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("capture Doppler summary must remain candidate-only")
        identities = tuple(item.recording_id for item in self.recordings)
        if len(identities) != len(set(identities)):
            raise ValueError("capture Doppler summary recordings must be unique")
        if self.original_recording_count < len(self.recordings):
            raise ValueError("capture Doppler summary count is inconsistent")
        if self.truncated != (self.original_recording_count > len(self.recordings)):
            raise ValueError("capture Doppler summary truncation is inconsistent")
        required = {
            "candidate-only-evidence-not-satellite-detection",
            "highest-score-selected-independently-per-authoritative-lnb-receiver",
            "radio-lnb-receiver-candidates-are-never-pooled",
        }
        if not required <= set(self.warnings):
            raise ValueError("capture Doppler summary lacks safety warnings")


class CaptureDopplerSummaryQueryPortV0_1(Protocol):
    def capture_doppler_summaries(
        self, query: CaptureDopplerSummaryQueryV0_1
    ) -> CaptureDopplerSummaryViewV0_1: ...
