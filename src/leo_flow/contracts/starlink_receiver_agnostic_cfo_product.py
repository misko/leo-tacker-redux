"""Durable recording product for receiver-agnostic CFO and symmetric QAM v0.6."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token
from .core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_adaptive_calibration import AdaptivePatternRole
from .starlink_receiver_agnostic_cfo import (
    MAXIMUM_CFO_PATTERNS,
    V0_6,
    ReceiverAgnosticCfoQamWindowBundleV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoWindowV0_6,
)
from .storage import ObjectRef, RecordingObjectRef

MAXIMUM_CFO_QAM_RECORDING_STREAMS = 2
MAXIMUM_CFO_QAM_WINDOWS_PER_STREAM = 3
MAXIMUM_CFO_QAM_RECORDING_WINDOWS = 6
MAXIMUM_CFO_QAM_PATTERN_EVIDENCE = 54
MAXIMUM_CFO_QAM_QUERY_WINDOWS = 6


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamRecordingPlanV0_6:
    search_plan: ReceiverAgnosticCfoSearchPlanV0_6
    maximum_streams: int = MAXIMUM_CFO_QAM_RECORDING_STREAMS
    maximum_windows_per_stream: int = MAXIMUM_CFO_QAM_WINDOWS_PER_STREAM
    maximum_patterns: int = 9
    maximum_pattern_evidence: int = MAXIMUM_CFO_QAM_PATTERN_EVIDENCE
    admission_mode: str = "explicit-offline-publication-only"

    def __post_init__(self) -> None:
        if (
            not 1 <= self.maximum_streams <= MAXIMUM_CFO_QAM_RECORDING_STREAMS
            or not 1
            <= self.maximum_windows_per_stream
            <= MAXIMUM_CFO_QAM_WINDOWS_PER_STREAM
            or not 1 <= self.maximum_patterns <= min(9, MAXIMUM_CFO_PATTERNS)
            or self.maximum_pattern_evidence
            != self.maximum_streams
            * self.maximum_windows_per_stream
            * self.maximum_patterns
            or self.admission_mode != "explicit-offline-publication-only"
        ):
            raise ValueError("receiver-agnostic CFO/QAM recording plan is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamRecordingRequestV0_6:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    plan: ReceiverAgnosticCfoQamRecordingPlanV0_6
    windows: tuple[ReceiverAgnosticCfoWindowV0_6, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.receiver-agnostic-cfo-qam-recording-request"

    def __post_init__(self) -> None:
        recording_digest = self.recording_object_ref.identity_digest()
        identities = tuple(item.identity for item in self.windows)
        stream_counts = Counter(
            (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
            for item in self.windows
        )
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_6)
            or self.recording_id != self.recording_object_ref.recording_id
            or self.requested_output_schema
            != SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6)
            or not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
            or len(identities) > MAXIMUM_CFO_QAM_RECORDING_WINDOWS
            or len(stream_counts) > self.plan.maximum_streams
            or any(
                count > self.plan.maximum_windows_per_stream
                for count in stream_counts.values()
            )
            or any(
                item.recording_id != self.recording_id
                or item.recording_identity_digest != recording_digest
                or item.source_recording_ref.digest != recording_digest
                for item in self.windows
            )
        ):
            raise ValueError("receiver-agnostic CFO/QAM recording request is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamRecordingBundleV0_6:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    plan: ReceiverAgnosticCfoQamRecordingPlanV0_6
    window_products: tuple[ReceiverAgnosticCfoQamWindowBundleV0_6, ...]
    provenance: Provenance
    candidates_only: bool
    calibrated_detection_count: None
    disclosures: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.receiver-agnostic-cfo-qam-recording-bundle"

    def __post_init__(self) -> None:
        required = {
            "candidate-evidence-not-calibrated-detection",
            "identical-residual-cfo-domain-for-every-radio-rx",
            "no-lnb-label-center-or-receiver-correction",
            "pattern-symmetric-known-pattern-qam",
            "explicit-offline-publication-only",
        }
        identities = tuple(item.window.identity for item in self.window_products)
        stream_counts = Counter(
            (
                item.window.radio_id,
                item.window.segment_id,
                item.window.receiver_chain_id,
                item.window.edge,
            )
            for item in self.window_products
        )
        evidence_count = sum(len(item.pattern_qam) for item in self.window_products)
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_6)
            or not self.analysis_id.startswith("slcfoqam6rec_")
            or not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
            or len(stream_counts) > self.plan.maximum_streams
            or any(
                count > self.plan.maximum_windows_per_stream
                for count in stream_counts.values()
            )
            or any(
                item.window.recording_id != self.recording_id
                or item.window.recording_identity_digest
                != self.recording_identity_digest
                or item.search_receipt.plan != self.plan.search_plan
                or len(item.pattern_qam) > self.plan.maximum_patterns
                or not item.candidates_only
                or item.calibrated_detection_count is not None
                for item in self.window_products
            )
            or evidence_count > self.plan.maximum_pattern_evidence
            or self.provenance.normalized_config_digest != self.plan.digest
            or not {item.digest for item in self.window_products}
            <= set(self.provenance.dependency_digests)
            or not self.candidates_only
            or self.calibrated_detection_count is not None
            or not required <= set(self.disclosures)
        ):
            raise ValueError("receiver-agnostic CFO/QAM recording bundle is invalid")

    @property
    def stream_count(self) -> int:
        return len(
            {
                (
                    item.window.radio_id,
                    item.window.segment_id,
                    item.window.receiver_chain_id,
                    item.window.edge,
                )
                for item in self.window_products
            }
        )

    @property
    def window_count(self) -> int:
        return len(self.window_products)

    @property
    def pattern_evidence_count(self) -> int:
        return sum(len(item.pattern_qam) for item in self.window_products)

    @property
    def unique_cell_count(self) -> int:
        return sum(
            item.search_receipt.unique_cell_count for item in self.window_products
        )

    @property
    def pattern_evaluation_count(self) -> int:
        return sum(
            item.search_receipt.pattern_evaluation_count
            for item in self.window_products
        )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamCatalogProjectionV0_6:
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    stream_count: int
    window_count: int
    pattern_evidence_count: int
    unique_cell_count: int
    pattern_evaluation_count: int
    candidates_only: bool

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if (
            not self.analysis_id.startswith("slcfoqam6rec_")
            or not 1 <= self.stream_count <= MAXIMUM_CFO_QAM_RECORDING_STREAMS
            or not self.stream_count
            <= self.window_count
            <= MAXIMUM_CFO_QAM_RECORDING_WINDOWS
            or not self.window_count
            <= self.pattern_evidence_count
            <= MAXIMUM_CFO_QAM_PATTERN_EVIDENCE
            or self.unique_cell_count < self.window_count
            or self.pattern_evaluation_count < self.unique_cell_count
            or not self.candidates_only
        ):
            raise ValueError("receiver-agnostic CFO/QAM catalog projection is invalid")


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamRecordingProductRefV0_6:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    @property
    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.bundle_ref.digest,
            SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6),
        )


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamQueryV0_6:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    maximum_windows: int = MAXIMUM_CFO_QAM_QUERY_WINDOWS

    def __post_init__(self) -> None:
        if (
            self.radio_ids != tuple(sorted(set(self.radio_ids)))
            or self.receiver_chain_ids != tuple(sorted(set(self.receiver_chain_ids)))
            or not 1 <= self.maximum_windows <= MAXIMUM_CFO_QAM_QUERY_WINDOWS
        ):
            raise ValueError("receiver-agnostic CFO/QAM query is invalid")


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamPatternSummaryV0_6:
    pattern_index: int
    role: AdaptivePatternRole
    template_ref: ArtifactRef
    winning_epoch_sample: int
    winning_cfo_hz: float
    winning_score: float
    complete_frame_count: int
    hard_symbol_accuracy: float
    rms_evm: float
    qam_goodness: float

    def __post_init__(self) -> None:
        for name in (
            "winning_cfo_hz",
            "winning_score",
            "hard_symbol_accuracy",
            "rms_evm",
            "qam_goodness",
        ):
            require_finite(getattr(self, name), name)
        expected = (
            AdaptivePatternRole.QIN
            if self.pattern_index == 0
            else AdaptivePatternRole.SURROGATE
        )
        if (
            self.pattern_index < 0
            or self.role is not expected
            or self.winning_epoch_sample < 0
            or not 0 <= self.winning_score <= 1
            or self.complete_frame_count <= 0
            or not 0 <= self.hard_symbol_accuracy <= 1
            or self.rms_evm < 0
            or not 0 <= self.qam_goodness <= 1
        ):
            raise ValueError("receiver-agnostic CFO/QAM pattern summary is invalid")


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamWindowSummaryV0_6:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    start_sample: int
    stop_sample: int
    sample_rate_hz: float
    cfo_min_hz: float
    cfo_max_hz: float
    coarse_cell_count: int
    local_cell_count: int
    unique_cell_count: int
    pattern_evaluation_count: int
    patterns: tuple[ReceiverAgnosticCfoQamPatternSummaryV0_6, ...]

    def __post_init__(self) -> None:
        for name in ("sample_rate_hz", "cfo_min_hz", "cfo_max_hz"):
            require_finite(getattr(self, name), name)
        if (
            self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or self.sample_rate_hz <= 0
            or self.cfo_min_hz >= self.cfo_max_hz
            or min(
                self.coarse_cell_count,
                self.local_cell_count,
                self.unique_cell_count,
                self.pattern_evaluation_count,
            )
            <= 0
            or tuple(item.pattern_index for item in self.patterns)
            != tuple(range(len(self.patterns)))
        ):
            raise ValueError("receiver-agnostic CFO/QAM window summary is invalid")


@dataclass(frozen=True)
class RecordingReceiverAgnosticCfoQamViewV0_6:
    schema: SchemaRef
    recording_id: RecordingId
    source_ref: ArtifactRef
    windows: tuple[ReceiverAgnosticCfoQamWindowSummaryV0_6, ...]
    total_window_count: int
    returned_window_count: int
    truncated: bool
    candidates_only: bool
    calibrated_detection_count: None
    limitations: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.recording-receiver-agnostic-cfo-qam-view"

    def __post_init__(self) -> None:
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_6)
            or self.returned_window_count != len(self.windows)
            or not 0 <= self.returned_window_count <= self.total_window_count
            or self.truncated != (self.returned_window_count < self.total_window_count)
            or not self.candidates_only
            or self.calibrated_detection_count is not None
            or self.limitations != tuple(sorted(set(self.limitations)))
        ):
            raise ValueError("recording receiver-agnostic CFO/QAM view is invalid")


class RecordingReceiverAgnosticCfoQamQueryPortV0_6(Protocol):
    def recording_receiver_agnostic_cfo_qam(
        self, query: ReceiverAgnosticCfoQamQueryV0_6
    ) -> RecordingReceiverAgnosticCfoQamViewV0_6: ...
