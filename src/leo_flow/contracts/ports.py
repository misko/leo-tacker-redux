"""Narrow public application ports connecting the three product boundaries."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, BinaryIO, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView, RecordingWriter

from .capture import (
    CapturePlan,
    CapturePlanRef,
    CompletedLocalRecording,
    SegmentManifest,
    SegmentRequest,
)
from .continuity import CaptureProvenance, ContinuityPolicy, RefillMetadata
from .core import (
    ArtifactRef,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    RecordingId,
    UtcNs,
)
from .dashboard import (
    ActivitySummary,
    FeatureView,
    ModelView,
    Page,
    RecordingDetail,
    RecordingSummary,
    StorageHealth,
    TimeRangeQuery,
    TrackView,
)
from .dwell import DwellRequest, ScanResultRef
from .ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSelection,
    EphemerisSnapshotCandidate,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
    RetrievalResult,
    ValidationResult,
)
from .evaluation import DetectorEvaluationView
from .features import (
    FeatureSetBundle,
    FeatureSetRef,
    RecordingAnalysisRequest,
)
from .hardware import HardwareMetadataSnapshot, HardwareMetadataSnapshotRef
from .model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotRef,
)
from .storage import ByteRange, ObjectRef, PublishedRecordingRef


class CapturePlanPublisher(Protocol):
    def publish(self, plan: CapturePlan, *, idempotency_key: str) -> CapturePlanRef: ...


class CapturePlanSource(Protocol):
    def get(self, plan_id: PlanId) -> CapturePlan: ...


class RadioDevice(Protocol):
    @property
    def radio_id(self) -> RadioId: ...

    def acquire_segment(
        self, request: SegmentRequest, write_ci16: Callable[[bytes], None]
    ) -> SegmentManifest: ...


class ContinuityRadioDevice(Protocol):
    """Metadata-aware acquisition without exposing a vendor driver type."""

    @property
    def radio_id(self) -> RadioId: ...

    @property
    def continuity_policy(self) -> ContinuityPolicy: ...

    @property
    def capture_provenance(self) -> CaptureProvenance: ...

    def acquire_segment_with_metadata(
        self,
        request: SegmentRequest,
        write_refill: Callable[[bytes, RefillMetadata | None], None],
    ) -> SegmentManifest: ...


class LocalSpool(Protocol):
    def allocate(self, plan_id: PlanId) -> tuple[RecordingId, str]: ...

    def record_complete(self, recording: CompletedLocalRecording) -> None: ...

    def record_failure(self, recording_id: RecordingId, reason: str) -> None: ...


class CaptureEngine(Protocol):
    def execute(
        self,
        plan: CapturePlan,
        hardware: RadioDevice,
        writer: RecordingWriter,
        spool: LocalSpool,
    ) -> CompletedLocalRecording: ...


class RecordingPublisher(Protocol):
    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef: ...


class RecordingAnalyzer(Protocol):
    def analyze(
        self, recording: RecordingView, request: RecordingAnalysisRequest
    ) -> FeatureSetBundle: ...


class DwellRequestEmitter(Protocol):
    """Analysis policy may propose a dwell using only a public scan result."""

    def emit(self, result: ScanResultRef) -> DwellRequest | None: ...


class DwellRequestGatePort(Protocol):
    """Capture-side validation turns an accepted request into a capture plan."""

    def accept(self, request: DwellRequest, now_utc_ns: UtcNs) -> CapturePlan: ...


class FeatureSetPublisher(Protocol):
    def publish(
        self,
        request: RecordingAnalysisRequest,
        bundle: FeatureSetBundle,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef: ...


class FeatureSetView(Protocol):
    @property
    def ref(self) -> FeatureSetRef: ...

    def bundle(self) -> FeatureSetBundle: ...


class FeatureSetReader(Protocol):
    def open(self, ref: FeatureSetRef) -> AbstractContextManager[FeatureSetView]: ...


class EphemerisView(Protocol):
    @property
    def ref(self) -> EphemerisSnapshotRef: ...

    def normalized_bytes(self) -> bytes: ...


class EphemerisReader(Protocol):
    def open(
        self, ref: EphemerisSnapshotRef
    ) -> AbstractContextManager[EphemerisView]: ...


class HardwareMetadataReader(Protocol):
    def get(self, ref: HardwareMetadataSnapshotRef) -> HardwareMetadataSnapshot: ...


class HardwareMetadataRefResolver(Protocol):
    """Resolve an immutable snapshot ID; never chooses a mutable latest value."""

    def resolve_ref(
        self, snapshot_id: HardwareSnapshotId
    ) -> HardwareMetadataSnapshotRef: ...


class HardwareMetadataPublisher(Protocol):
    def publish(
        self, snapshot: HardwareMetadataSnapshot, *, idempotency_key: str
    ) -> HardwareMetadataSnapshotRef: ...


class ModelFitter(Protocol):
    def fit(
        self,
        request: ModelAnalysisRequest,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
    ) -> ModelSnapshotBundle: ...


class ModelPublisher(Protocol):
    def publish(
        self,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef: ...


class ModelSnapshotView(Protocol):
    @property
    def ref(self) -> ModelSnapshotRef: ...

    def bundle(self) -> ModelSnapshotBundle: ...


class ModelSnapshotReader(Protocol):
    def open(
        self, ref: ModelSnapshotRef
    ) -> AbstractContextManager[ModelSnapshotView]: ...


class ModelReleasePublisher(Protocol):
    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease: ...


class EphemerisRetriever(Protocol):
    def fetch(self, request: EphemerisRetrievalRequest) -> RetrievalResult: ...


class EphemerisNormalizer(Protocol):
    def normalize(
        self, raw_ref: ObjectRef, parser_ref: ArtifactRef
    ) -> EphemerisSnapshotCandidate: ...


class EphemerisValidator(Protocol):
    def validate(
        self, candidate: EphemerisSnapshotCandidate, policy_ref: ArtifactRef
    ) -> ValidationResult: ...


class EphemerisPublisher(Protocol):
    def publish(
        self,
        retrieval: RetrievalResult,
        candidate: EphemerisSnapshotCandidate,
        validation: ValidationResult,
        *,
        idempotency_key: str,
    ) -> EphemerisSnapshotRef: ...


class EphemerisResolver(Protocol):
    def resolve(
        self,
        source: EphemerisSource,
        recording_interval: RecordingInterval,
        policy_ref: ArtifactRef,
        as_of_utc_ns: UtcNs,
    ) -> EphemerisSelection: ...


class DashboardQueryPort(Protocol):
    def recent_recordings(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[RecordingSummary]: ...

    def activity(self, query: TimeRangeQuery) -> ActivitySummary: ...

    def recording_detail(self, recording_id: RecordingId) -> RecordingDetail: ...

    def recording_features(
        self, recording_id: RecordingId, selector: str, cursor: str | None = None
    ) -> Page[FeatureView]: ...

    def model_snapshot(self, model_id_or_release_alias: str) -> ModelView: ...

    def detector_evaluation(
        self, evaluation_id_or_run_id: str
    ) -> DetectorEvaluationView: ...

    def tracks(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[TrackView]: ...

    def storage_health(self) -> StorageHealth: ...


class DiagnosticArtifactReader(Protocol):
    def open(
        self, ref: ObjectRef, byte_range: ByteRange | None = None
    ) -> AbstractContextManager[BinaryIO]: ...
