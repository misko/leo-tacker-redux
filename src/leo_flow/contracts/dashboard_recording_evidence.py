"""Additive recording-evidence context for the interactive V16 workspace.

The context carries identities and selector dimensions only.  Evidence remains
owned by its versioned QAM, detector, and Doppler query ports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    V0_1,
    CaptureBatchId,
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)

MAXIMUM_EVIDENCE_RECORDINGS = 2
MAXIMUM_EVIDENCE_RECEIVERS = 16
MAXIMUM_EVIDENCE_SEGMENTS = 32
MAXIMUM_DOPPLER_WINDOW_ESTIMATES = 4096


@dataclass(frozen=True)
class RecordingEvidenceRecordingV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    radio_serial: str
    hardware_snapshot_id: HardwareSnapshotId
    capture_started_utc_ns: UtcNs
    capture_finished_utc_ns: UtcNs
    analysis_state: str
    requested: bool

    def __post_init__(self) -> None:
        require_token(self.radio_serial, "radio_serial")
        require_token(self.analysis_state, "analysis_state")
        require_utc_ns(self.capture_started_utc_ns, "capture_started_utc_ns")
        require_utc_ns(self.capture_finished_utc_ns, "capture_finished_utc_ns")
        if self.capture_finished_utc_ns <= self.capture_started_utc_ns:
            raise ValueError("evidence recording interval must be non-empty")
        if not isinstance(self.requested, bool):
            raise TypeError("requested must be a boolean")


@dataclass(frozen=True)
class RecordingEvidenceReceiverV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    radio_channel: int
    lnb_id: str
    polarization: str | None
    valid_from_utc_ns: UtcNs
    valid_until_utc_ns: UtcNs | None

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        if self.polarization is not None:
            require_token(self.polarization, "polarization")
        if self.radio_channel < 0:
            raise ValueError("radio_channel must be non-negative")
        require_utc_ns(self.valid_from_utc_ns, "valid_from_utc_ns")
        if self.valid_until_utc_ns is not None:
            require_utc_ns(self.valid_until_utc_ns, "valid_until_utc_ns")
            if self.valid_until_utc_ns <= self.valid_from_utc_ns:
                raise ValueError("receiver assignment interval must be non-empty")


@dataclass(frozen=True)
class RecordingEvidenceSegmentV0_1:
    recording_id: RecordingId
    segment_id: SegmentId
    receiver_chain_ids: tuple[ReceiverChainId, ...]

    def __post_init__(self) -> None:
        if not self.receiver_chain_ids:
            raise ValueError("evidence segment requires a receiver")
        if len(set(self.receiver_chain_ids)) != len(self.receiver_chain_ids):
            raise ValueError("evidence segment receivers must be unique")


@dataclass(frozen=True)
class RecordingEvidenceContextViewV0_1:
    schema: SchemaRef
    requested_recording_id: RecordingId
    capture_batch_id: CaptureBatchId | None
    recordings: tuple[RecordingEvidenceRecordingV0_1, ...]
    receivers: tuple[RecordingEvidenceReceiverV0_1, ...]
    segments: tuple[RecordingEvidenceSegmentV0_1, ...]
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-evidence-context"
    CANDIDATE_WARNING = "candidate-only-evidence-not-calibrated-detection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording-evidence context schema")
        if not 1 <= len(self.recordings) <= MAXIMUM_EVIDENCE_RECORDINGS:
            raise ValueError("recording-evidence recording count is out of bounds")
        recording_ids = tuple(item.recording_id for item in self.recordings)
        if len(set(recording_ids)) != len(recording_ids):
            raise ValueError("recording-evidence recordings must be unique")
        requested = tuple(item for item in self.recordings if item.requested)
        if (
            len(requested) != 1
            or requested[0].recording_id != self.requested_recording_id
        ):
            raise ValueError(
                "recording-evidence context requires one requested recording"
            )
        if len(self.receivers) > MAXIMUM_EVIDENCE_RECEIVERS:
            raise ValueError("recording-evidence receiver count is out of bounds")
        receiver_keys = tuple(
            (item.recording_id, item.receiver_chain_id) for item in self.receivers
        )
        if len(set(receiver_keys)) != len(receiver_keys):
            raise ValueError("recording-evidence receiver assignments must be unique")
        if any(item.recording_id not in recording_ids for item in self.receivers):
            raise ValueError("receiver assignment belongs to another recording")
        if len(self.segments) > MAXIMUM_EVIDENCE_SEGMENTS:
            raise ValueError("recording-evidence segment count is out of bounds")
        segment_keys = tuple(
            (item.recording_id, item.segment_id) for item in self.segments
        )
        if len(set(segment_keys)) != len(segment_keys):
            raise ValueError("recording-evidence segments must be unique")
        if any(item.recording_id not in recording_ids for item in self.segments):
            raise ValueError("segment belongs to another recording")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("recording evidence must remain candidate-only")
        if self.CANDIDATE_WARNING not in self.warnings:
            raise ValueError("recording evidence requires a candidate-only warning")
        for values, label in (
            (self.warnings, "warnings"),
            (self.limitations, "limitations"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be unique and canonical")
            for value in values:
                require_token(value, label)


class RecordingEvidenceContextQueryPortV0_1(Protocol):
    def recording_evidence_context(
        self, recording_id: RecordingId
    ) -> RecordingEvidenceContextViewV0_1: ...


@dataclass(frozen=True)
class RecordingEvidenceDopplerQueryV0_1:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    maximum_windows: int = 1024

    def __post_init__(self) -> None:
        for values, label in (
            (self.radio_ids, "radios"),
            (self.lnb_ids, "LNBs"),
            (self.receiver_chain_ids, "receivers"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Doppler query {label} must be unique")
        for lnb_id in self.lnb_ids:
            require_token(lnb_id, "lnb_id")
        if not 1 <= self.maximum_windows <= MAXIMUM_DOPPLER_WINDOW_ESTIMATES:
            raise ValueError("Doppler query window bound is invalid")


@dataclass(frozen=True)
class RecordingEvidenceDopplerTotalV0_1:
    drift_rate_hz_s: float
    drift_acceleration_hz_s2: float
    reference_utc_ns: UtcNs
    reference_frequency_hz: float
    support_count: int
    residual_rms_hz: float
    derivation: str

    def __post_init__(self) -> None:
        for name in (
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "reference_frequency_hz",
            "residual_rms_hz",
        ):
            require_finite(getattr(self, name), name)
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        if self.support_count < 2 or self.residual_rms_hz < 0:
            raise ValueError("invalid total Doppler fit support")
        if self.derivation != "published-blind-doppler-candidate-fit":
            raise ValueError("unknown total Doppler derivation")


@dataclass(frozen=True)
class RecordingEvidenceDopplerWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    drift_rate_hz_s: float
    midpoint_frequency_hz: float
    support_count: int
    derivation: str

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid Doppler window sample interval")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid Doppler window UTC interval")
        require_finite(self.drift_rate_hz_s, "drift_rate_hz_s")
        require_finite(self.midpoint_frequency_hz, "midpoint_frequency_hz")
        if self.support_count != 2:
            raise ValueError("adjacent-point Doppler windows require two points")
        if self.derivation != "adjacent-published-track-points-linear-slope":
            raise ValueError("unknown window Doppler derivation")


@dataclass(frozen=True)
class RecordingEvidenceDopplerSeriesV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    candidate_rank: int
    selected_model: str
    provenance_artifact_id: str
    total: RecordingEvidenceDopplerTotalV0_1
    windows: tuple[RecordingEvidenceDopplerWindowV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_token(self.selected_model, "selected_model")
        require_token(self.provenance_artifact_id, "provenance_artifact_id")
        if self.candidate_rank < 1 or not self.windows:
            raise ValueError("Doppler series requires a ranked candidate and windows")
        if tuple(item.window_index for item in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("Doppler windows must be canonical")


@dataclass(frozen=True)
class RecordingEvidenceDopplerViewV0_1:
    schema: SchemaRef
    requested_recording_id: RecordingId
    state: str
    candidate_only: bool
    calibrated_detection_count: None
    series: tuple[RecordingEvidenceDopplerSeriesV0_1, ...]
    original_window_count: int
    truncated: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-evidence-doppler"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording-evidence Doppler schema")
        if self.state not in {"complete", "pending", "missing", "error"}:
            raise ValueError("invalid recording-evidence Doppler state")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("recording Doppler evidence must remain candidate-only")
        if self.original_window_count < sum(len(item.windows) for item in self.series):
            raise ValueError("Doppler original window count is inconsistent")
        if self.truncated != (
            self.original_window_count > sum(len(item.windows) for item in self.series)
        ):
            raise ValueError("Doppler truncation flag is inconsistent")
        if self.state == "complete" and not self.series:
            raise ValueError("complete Doppler evidence requires series")
        if self.state != "complete" and self.series:
            raise ValueError("incomplete Doppler evidence cannot expose series")
        if tuple(sorted(set(self.warnings))) != self.warnings:
            raise ValueError("Doppler warnings must be unique and canonical")


class RecordingEvidenceDopplerQueryPortV0_1(Protocol):
    def recording_evidence_doppler(
        self, query: RecordingEvidenceDopplerQueryV0_1
    ) -> RecordingEvidenceDopplerViewV0_1: ...
