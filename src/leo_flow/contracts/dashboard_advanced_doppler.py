"""Additive dashboard contracts for advanced-path-only Doppler evidence.

V16's published basic-candidate response remains immutable.  These contracts
expose the independently published advanced slope-bank path, including the
exact waterfall rows from which local physical drift rates are derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from .dashboard_recording_evidence import RecordingEvidenceDopplerQueryV0_1


@dataclass(frozen=True)
class PublishedAdvancedDopplerPathPointV0_1:
    row_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    midpoint_utc_ns: UtcNs
    frequency_hz: float

    def __post_init__(self) -> None:
        if (
            self.row_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid advanced Doppler path point scope")
        for name in (
            "interval_start_utc_ns",
            "interval_stop_utc_ns",
            "midpoint_utc_ns",
        ):
            require_utc_ns(getattr(self, name), name)
        if not (
            self.interval_start_utc_ns
            < self.midpoint_utc_ns
            < self.interval_stop_utc_ns
        ):
            raise ValueError("advanced Doppler point midpoint is outside its scope")
        require_finite(self.frequency_hz, "frequency_hz")


@dataclass(frozen=True)
class PublishedAdvancedDopplerPathV0_1:
    recording_id: RecordingId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    path_digest: Digest
    provenance_artifact_id: str
    association_state: str
    published_drift_rate_hz_s: float
    points: tuple[PublishedAdvancedDopplerPathPointV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.provenance_artifact_id, "provenance_artifact_id")
        if self.association_state != "advanced-path-only":
            raise ValueError("only unassociated advanced paths belong in this view")
        require_finite(self.published_drift_rate_hz_s, "published_drift_rate_hz_s")
        if len(self.points) < 2:
            raise ValueError("advanced Doppler path requires at least two points")
        if tuple(point.row_index for point in self.points) != tuple(
            range(len(self.points))
        ):
            raise ValueError("advanced Doppler path rows must be canonical")
        if any(
            later.midpoint_utc_ns <= earlier.midpoint_utc_ns
            for earlier, later in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("advanced Doppler path time must be increasing")


class PublishedAdvancedDopplerPathQueryPortV0_1(Protocol):
    def recording_advanced_doppler_paths(
        self, recording_id: RecordingId
    ) -> tuple[PublishedAdvancedDopplerPathV0_1, ...]: ...


@dataclass(frozen=True)
class RecordingEvidenceAdvancedDopplerTotalV0_1:
    drift_rate_hz_s: float
    reference_utc_ns: UtcNs
    reference_frequency_hz: float
    support_count: int
    residual_rms_hz: float
    derivation: str

    def __post_init__(self) -> None:
        for name in ("drift_rate_hz_s", "reference_frequency_hz", "residual_rms_hz"):
            require_finite(getattr(self, name), name)
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        if self.support_count < 2 or self.residual_rms_hz < 0:
            raise ValueError("invalid advanced Doppler total support")
        if self.derivation != "published-advanced-slope-bank-path-rate":
            raise ValueError("unknown advanced Doppler total derivation")


@dataclass(frozen=True)
class RecordingEvidenceAdvancedDopplerWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    point_start_utc_ns: UtcNs
    point_stop_utc_ns: UtcNs
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
            raise ValueError("invalid advanced Doppler window sample scope")
        for name in (
            "interval_start_utc_ns",
            "interval_stop_utc_ns",
            "point_start_utc_ns",
            "point_stop_utc_ns",
        ):
            require_utc_ns(getattr(self, name), name)
        if not (
            self.interval_start_utc_ns
            < self.point_start_utc_ns
            < self.point_stop_utc_ns
            < self.interval_stop_utc_ns
        ):
            raise ValueError("advanced Doppler window point times exceed exact scope")
        require_finite(self.drift_rate_hz_s, "drift_rate_hz_s")
        require_finite(self.midpoint_frequency_hz, "midpoint_frequency_hz")
        if self.support_count != 2:
            raise ValueError("advanced path windows require two published points")
        if self.derivation != "adjacent-published-advanced-path-points-linear-slope":
            raise ValueError("unknown advanced Doppler window derivation")


@dataclass(frozen=True)
class RecordingEvidenceAdvancedDopplerSeriesV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    path_digest: Digest
    provenance_artifact_id: str
    association_state: str
    total: RecordingEvidenceAdvancedDopplerTotalV0_1
    windows: tuple[RecordingEvidenceAdvancedDopplerWindowV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_token(self.provenance_artifact_id, "provenance_artifact_id")
        if self.association_state != "advanced-path-only" or not self.windows:
            raise ValueError("advanced path series requires unassociated path windows")
        if tuple(window.window_index for window in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("advanced path windows must be canonical")


@dataclass(frozen=True)
class RecordingEvidenceAdvancedDopplerViewV0_1:
    schema: SchemaRef
    requested_recording_id: RecordingId
    state: str
    candidate_only: bool
    calibrated_detection_count: None
    series: tuple[RecordingEvidenceAdvancedDopplerSeriesV0_1, ...]
    original_window_count: int
    truncated: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-evidence-advanced-doppler"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported advanced Doppler evidence schema")
        if self.state not in {"complete", "pending", "missing", "error"}:
            raise ValueError("invalid advanced Doppler evidence state")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("advanced Doppler evidence must remain candidate-only")
        shown = sum(len(item.windows) for item in self.series)
        if self.original_window_count < shown or self.truncated != (
            self.original_window_count > shown
        ):
            raise ValueError("advanced Doppler truncation accounting is inconsistent")
        if (self.state == "complete") != bool(self.series):
            raise ValueError("advanced Doppler state and series disagree")
        if tuple(sorted(set(self.warnings))) != self.warnings:
            raise ValueError("advanced Doppler warnings must be canonical")


class RecordingEvidenceAdvancedDopplerQueryPortV0_1(Protocol):
    def recording_evidence_advanced_doppler(
        self, query: RecordingEvidenceDopplerQueryV0_1
    ) -> RecordingEvidenceAdvancedDopplerViewV0_1: ...
