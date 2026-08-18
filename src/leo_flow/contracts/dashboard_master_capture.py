"""Immutable page-load snapshot for the dashboard capture table."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias
from urllib.parse import quote

from ._validation import require_token, require_utc_ns
from .capture_batch import CaptureBatchMode, PairedAnalysisEligibility
from .core import (
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from .dashboard_batch import (
    CaptureAttemptDashboardView,
    CaptureBatchDashboardView,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from .dashboard_capture_doppler import CaptureDopplerCandidateSummaryV0_1
from .dashboard_capture_qam import CaptureQamCandidateSummaryV0_1
from .dashboard_observation import ObservationAggregateViewV0_1
from .dashboard_retro_qam_canary import RetroQamCanaryDashboardViewV0_1

MAX_MASTER_CAPTURE_RECORDINGS = 100

MasterCaptureQamCandidateV0_1: TypeAlias = CaptureQamCandidateSummaryV0_1
MasterCaptureDopplerCandidateV0_1: TypeAlias = CaptureDopplerCandidateSummaryV0_1


class MasterCaptureSummaryState(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
    NO_CANDIDATE = "no_candidate"
    NOT_ANALYZED = "not_analyzed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MasterCaptureSnapshotQueryV0_1:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    maximum_recordings: int = MAX_MASTER_CAPTURE_RECORDINGS

    def __post_init__(self) -> None:
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("master capture interval must be non-empty")
        if not 1 <= self.maximum_recordings <= MAX_MASTER_CAPTURE_RECORDINGS:
            raise ValueError("master capture recording bound is invalid")


def _validate_reasons(reason_codes: tuple[str, ...]) -> None:
    if reason_codes != tuple(sorted(set(reason_codes))):
        raise ValueError("master capture reason codes must be canonical")
    for reason in reason_codes:
        require_token(reason, "reason_code")


@dataclass(frozen=True)
class MasterCaptureQamV0_1:
    state: MasterCaptureSummaryState
    candidates: tuple[MasterCaptureQamCandidateV0_1, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_summary(self.state, self.candidates, self.reason_codes, "QAM")
        identities = tuple(
            (item.lnb_id, item.receiver_chain_id) for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError("master capture QAM candidates must remain unpooled")


@dataclass(frozen=True)
class MasterCaptureDopplerV0_1:
    state: MasterCaptureSummaryState
    candidates: tuple[MasterCaptureDopplerCandidateV0_1, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_summary(self.state, self.candidates, self.reason_codes, "Doppler")
        identities = tuple(
            (item.lnb_id, item.receiver_chain_id) for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError("master capture Doppler candidates must remain unpooled")


def _validate_summary(
    state: MasterCaptureSummaryState,
    candidates: tuple[object, ...],
    reason_codes: tuple[str, ...],
    name: str,
) -> None:
    if not isinstance(state, MasterCaptureSummaryState):
        raise TypeError("master capture summary state is invalid")
    if state is MasterCaptureSummaryState.COMPLETE and not candidates:
        raise ValueError(f"complete {name} summary requires candidates")
    if state is not MasterCaptureSummaryState.COMPLETE and candidates:
        raise ValueError(f"incomplete {name} summary cannot expose candidates")
    _validate_reasons(reason_codes)


@dataclass(frozen=True)
class MasterCapturePilotV0_1:
    state: MasterCaptureSummaryState
    anchor_8_detection_count: int | None
    glrt_detection_count: int | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_count_summary(
            self.state,
            (self.anchor_8_detection_count, self.glrt_detection_count),
            self.reason_codes,
            "pilot",
        )


@dataclass(frozen=True)
class MasterCaptureSatelliteV0_1:
    state: MasterCaptureSummaryState
    count: int | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_count_summary(
            self.state, (self.count,), self.reason_codes, "satellite"
        )


def _validate_count_summary(
    state: MasterCaptureSummaryState,
    counts: tuple[int | None, ...],
    reason_codes: tuple[str, ...],
    name: str,
) -> None:
    if not isinstance(state, MasterCaptureSummaryState):
        raise TypeError(f"master capture {name} state is invalid")
    if state is MasterCaptureSummaryState.COMPLETE:
        if any(
            value is None
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counts
        ):
            raise ValueError(f"complete {name} summary requires non-negative counts")
    elif any(value is not None for value in counts):
        raise ValueError(f"incomplete {name} summary cannot expose counts")
    _validate_reasons(reason_codes)


@dataclass(frozen=True)
class MasterCaptureAttemptV0_1:
    attempt_id: CaptureAttemptId
    radio_id: RadioId
    plan_id: PlanId
    requested_start_utc_ns: UtcNs
    capture_state: DashboardCaptureState
    observed_start_utc_ns: UtcNs | None
    recording_id: RecordingId | None
    failure_reason: str | None
    analysis_state: DashboardAnalysisState
    analysis_result_available: bool
    detail_href: str | None
    capture_duration_ns: int | None
    qam: MasterCaptureQamV0_1
    doppler: MasterCaptureDopplerV0_1
    pilot: MasterCapturePilotV0_1
    satellites: MasterCaptureSatelliteV0_1

    def __post_init__(self) -> None:
        CaptureAttemptDashboardView(
            self.attempt_id,
            self.radio_id,
            self.plan_id,
            self.requested_start_utc_ns,
            self.capture_state,
            self.observed_start_utc_ns,
            self.recording_id,
            self.failure_reason,
            self.analysis_state,
            self.analysis_result_available,
        )
        expected_href = (
            None
            if self.recording_id is None
            else f"/recordings/{quote(str(self.recording_id), safe='')}"
        )
        if self.detail_href != expected_href:
            raise ValueError(
                "master capture detail link differs from recording identity"
            )
        if self.capture_duration_ns is not None and (
            isinstance(self.capture_duration_ns, bool)
            or not isinstance(self.capture_duration_ns, int)
            or self.capture_duration_ns <= 0
        ):
            raise ValueError("capture duration must be a positive integer")
        if self.recording_id is None and self.capture_duration_ns is not None:
            raise ValueError("capture without recording cannot expose duration")
        for qam_candidate in self.qam.candidates:
            if (
                qam_candidate.recording_id != self.recording_id
                or qam_candidate.radio_id != self.radio_id
            ):
                raise ValueError("master capture candidate belongs to another attempt")
        for doppler_candidate in self.doppler.candidates:
            if (
                doppler_candidate.recording_id != self.recording_id
                or doppler_candidate.radio_id != self.radio_id
            ):
                raise ValueError("master capture candidate belongs to another attempt")


@dataclass(frozen=True)
class MasterCaptureBatchV0_1:
    batch_id: CaptureBatchId
    mode: CaptureBatchMode
    coordination_claim: CoordinationClaim
    attempts: tuple[MasterCaptureAttemptV0_1, MasterCaptureAttemptV0_1]
    revision: int
    requested_start_skew_ns: int
    observed_start_skew_ns: int | None
    maximum_observed_start_skew_ns: int | None
    paired_analysis_eligibility: PairedAnalysisEligibility

    def __post_init__(self) -> None:
        CaptureBatchDashboardView(
            SchemaRef(CaptureBatchDashboardView.SCHEMA_ID),
            self.batch_id,
            self.mode,
            self.coordination_claim,
            tuple(
                CaptureAttemptDashboardView(
                    item.attempt_id,
                    item.radio_id,
                    item.plan_id,
                    item.requested_start_utc_ns,
                    item.capture_state,
                    item.observed_start_utc_ns,
                    item.recording_id,
                    item.failure_reason,
                    item.analysis_state,
                    item.analysis_result_available,
                )
                for item in self.attempts
            ),  # type: ignore[arg-type]
            self.revision,
            self.requested_start_skew_ns,
            self.observed_start_skew_ns,
            self.maximum_observed_start_skew_ns,
            self.paired_analysis_eligibility,
        )


@dataclass(frozen=True)
class MasterCaptureObservationV0_1:
    state: MasterCaptureSummaryState
    value: ObservationAggregateViewV0_1 | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_optional_value(
            self.state, self.value, self.reason_codes, "observation"
        )


@dataclass(frozen=True)
class MasterCaptureRetroQamCanaryV0_1:
    state: MasterCaptureSummaryState
    value: RetroQamCanaryDashboardViewV0_1 | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_optional_value(self.state, self.value, self.reason_codes, "canary")


def _validate_optional_value(
    state: MasterCaptureSummaryState,
    value: object | None,
    reason_codes: tuple[str, ...],
    name: str,
) -> None:
    if (state is MasterCaptureSummaryState.COMPLETE) != (value is not None):
        raise ValueError(f"master capture {name} value and state differ")
    _validate_reasons(reason_codes)


@dataclass(frozen=True)
class MasterCaptureSnapshotV0_1:
    schema_version: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    items: tuple[MasterCaptureBatchV0_1, ...]
    next_cursor: str | None
    observation_aggregate: MasterCaptureObservationV0_1
    retro_qam_canary: MasterCaptureRetroQamCanaryV0_1
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported master capture snapshot schema")
        require_utc_ns(self.start_utc_ns, "start_utc_ns")
        require_utc_ns(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("master capture snapshot interval must be non-empty")
        batch_ids = tuple(item.batch_id for item in self.items)
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("master capture batches must be unique")
        required = {
            "candidate-only-qam-goodness-not-starlink-detection",
            "radio-lnb-receiver-series-are-never-pooled",
        }
        if not required <= set(self.warnings):
            raise ValueError("master capture snapshot lacks safety warnings")


class MasterCaptureSnapshotQueryPortV0_1(Protocol):
    def master_capture_snapshot(
        self, query: MasterCaptureSnapshotQueryV0_1, cursor: str | None = None
    ) -> MasterCaptureSnapshotV0_1: ...


class CaptureQamSnapshotQueryPortV0_1(Protocol):
    """One bounded stored-summary read; absence is not a terminal outcome."""

    def capture_qam_snapshot(
        self,
        query: MasterCaptureSnapshotQueryV0_1,
        recording_ids: tuple[RecordingId, ...],
    ) -> Mapping[RecordingId, MasterCaptureQamV0_1]: ...


class CaptureDopplerSnapshotQueryPortV0_1(Protocol):
    """One bounded stored-summary read; implementations must never open CAS."""

    def capture_doppler_snapshot(
        self,
        query: MasterCaptureSnapshotQueryV0_1,
        recording_ids: tuple[RecordingId, ...],
    ) -> Mapping[RecordingId, MasterCaptureDopplerV0_1]: ...
