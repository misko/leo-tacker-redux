"""Bounded candidate-only QAM summaries for the master capture table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import RadioId, ReceiverChainId, RecordingId, SegmentId, UtcNs
from .starlink import StarlinkEdge

MAX_CAPTURE_QAM_RECORDINGS = 100
MAX_CAPTURE_QAM_SERIES_PER_RECORDING = 16


class CaptureQamState(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class CaptureQamSummaryQueryV0_1:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    maximum_recordings: int = MAX_CAPTURE_QAM_RECORDINGS

    def __post_init__(self) -> None:
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("capture QAM interval must be non-empty")
        if not 1 <= self.maximum_recordings <= MAX_CAPTURE_QAM_RECORDINGS:
            raise ValueError("capture QAM recording bound is invalid")


@dataclass(frozen=True)
class CaptureQamCandidateSummaryV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    edge: StarlinkEdge
    qam_goodness: float
    hard_symbol_accuracy: float
    rms_evm: float
    window_count: int
    analysis_id: str
    selection: str = "highest-qam-goodness-per-recording-radio-lnb-receiver"

    def __post_init__(self) -> None:
        for token_value, name in (
            (self.lnb_id, "lnb_id"),
            (self.analysis_id, "analysis_id"),
        ):
            require_token(token_value, name)
        for metric_value, name in (
            (self.qam_goodness, "qam_goodness"),
            (self.hard_symbol_accuracy, "hard_symbol_accuracy"),
            (self.rms_evm, "rms_evm"),
        ):
            require_finite(metric_value, name)
        if (
            not 0 <= self.qam_goodness <= 1
            or not 0 <= self.hard_symbol_accuracy <= 1
            or self.rms_evm < 0
            or self.window_count <= 0
        ):
            raise ValueError("capture QAM candidate metrics are invalid")
        if self.selection != "highest-qam-goodness-per-recording-radio-lnb-receiver":
            raise ValueError("unsupported capture QAM selection")


@dataclass(frozen=True)
class CaptureQamRecordingSummaryV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    analysis_state: str
    state: CaptureQamState
    candidates: tuple[CaptureQamCandidateSummaryV0_1, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        require_token(self.analysis_state, "analysis_state")
        if not isinstance(self.state, CaptureQamState):
            raise TypeError("state must be CaptureQamState")
        if self.state is CaptureQamState.COMPLETE and not self.candidates:
            raise ValueError("complete capture QAM summary requires candidates")
        if self.state is not CaptureQamState.COMPLETE and self.candidates:
            raise ValueError("incomplete capture QAM summary cannot expose candidates")
        if len(self.candidates) > MAX_CAPTURE_QAM_SERIES_PER_RECORDING:
            raise ValueError("capture QAM candidate count is out of bounds")
        identities = tuple(
            (item.lnb_id, item.receiver_chain_id) for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError("capture QAM candidates must not pool receiver identity")
        if any(
            item.recording_id != self.recording_id or item.radio_id != self.radio_id
            for item in self.candidates
        ):
            raise ValueError("capture QAM candidate belongs to another row")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("capture QAM reasons must be unique and canonical")


@dataclass(frozen=True)
class CaptureQamSummaryViewV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    candidate_only: bool
    calibration_required: bool
    calibrated_detection_count: None
    recordings: tuple[CaptureQamRecordingSummaryV0_1, ...]
    original_recording_count: int
    truncated: bool
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported capture QAM summary schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("capture QAM summary interval must be non-empty")
        if (
            not self.candidate_only
            or not self.calibration_required
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("capture QAM summary safety semantics differ")
        identities = tuple(item.recording_id for item in self.recordings)
        if len(identities) != len(set(identities)):
            raise ValueError("capture QAM summary recordings must be unique")
        if self.original_recording_count < len(self.recordings):
            raise ValueError("capture QAM summary count is inconsistent")
        if self.truncated != (self.original_recording_count > len(self.recordings)):
            raise ValueError("capture QAM summary truncation is inconsistent")
        required = {
            "candidate-only-qam-goodness-not-starlink-detection",
            "highest-goodness-selected-independently-per-authoritative-lnb-receiver",
            "radio-lnb-receiver-series-are-never-pooled",
        }
        if not required <= set(self.warnings):
            raise ValueError("capture QAM summary lacks safety warnings")


class CaptureQamSummaryQueryPortV0_1(Protocol):
    def capture_qam_summaries(
        self, query: CaptureQamSummaryQueryV0_1
    ) -> CaptureQamSummaryViewV0_1: ...
