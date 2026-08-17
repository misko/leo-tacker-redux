"""Durable and query boundaries for Starlink paired-surrogate evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import REPORT_METHOD_ORDER, StarlinkDetectorMethod
from .starlink_surrogate_null import (
    MAXIMUM_SURROGATES,
    V0_1,
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkSearchGridV0_1,
    StarlinkSearchPatternV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

MAXIMUM_SURROGATE_NULL_STREAMS = 64
MAXIMUM_SURROGATE_NULL_QUERY_ROWS = MAXIMUM_SURROGATE_NULL_STREAMS * len(
    REPORT_METHOD_ORDER
)


class StarlinkSurrogateNullRecordingState(str, Enum):
    CANDIDATES = "candidates"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class StarlinkSurrogateNullStreamSelectionV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0:
            raise ValueError("selection sample rate must be positive")
        if (
            isinstance(self.probe_sample_count, bool)
            or not isinstance(self.probe_sample_count, int)
            or self.probe_sample_count <= 0
        ):
            raise ValueError("selection probe_sample_count must be positive")


@dataclass(frozen=True)
class StarlinkSurrogateNullRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    surrogate_count: int
    stream_selections: tuple[StarlinkSurrogateNullStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef
    ineligible_reason: str | None = None

    SCHEMA_ID = "org.leo-flow.starlink-surrogate-null-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported surrogate-null request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("surrogate-null request recording identities differ")
        if self.requested_output_schema != SchemaRef(
            StarlinkSurrogateNullRecordingBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported surrogate-null output schema")
        if (
            isinstance(self.surrogate_count, bool)
            or not isinstance(self.surrogate_count, int)
            or not 1 <= self.surrogate_count <= MAXIMUM_SURROGATES
        ):
            raise ValueError("surrogate_count must lie in [1,32]")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id) for item in self.stream_selections
        )
        if keys != tuple(sorted(keys, key=lambda item: (str(item[0]), str(item[1])))):
            raise ValueError("surrogate-null selections must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_SURROGATE_NULL_STREAMS:
            raise ValueError("surrogate-null selections are duplicate or unbounded")
        if self.ineligible_reason is None:
            if not self.stream_selections:
                raise ValueError("eligible surrogate-null request requires streams")
        elif self.ineligible_reason != "clipped-pilot-band":
            raise ValueError("unknown surrogate-null ineligibility reason")
        elif self.stream_selections:
            raise ValueError("ineligible surrogate-null request cannot select streams")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSurrogateNullStreamEvidenceV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    evidence: StarlinkPairedSurrogateEvidenceV0_1

    def __post_init__(self) -> None:
        if isinstance(self.channel_number, bool) or self.channel_number not in (
            1,
            2,
            3,
            4,
        ):
            raise ValueError("channel_number must be one of 1,2,3,4")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("surrogate-null interval must be non-empty")
        exact = self.evidence.exact
        if (
            exact.segment_id != self.segment_id
            or exact.receiver_chain_id != self.receiver_chain_id
            or exact.edge is not self.edge
        ):
            raise ValueError("stream context and paired evidence differ")


@dataclass(frozen=True)
class StarlinkSurrogateNullRecordingBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    state: StarlinkSurrogateNullRecordingState
    streams: tuple[StarlinkSurrogateNullStreamEvidenceV0_1, ...]
    reason_codes: tuple[str, ...]
    calibrated_detection_count: int | None

    SCHEMA_ID = "org.leo-flow.starlink-surrogate-null-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported surrogate-null recording schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slsnullrec_"):
            raise ValueError("invalid surrogate-null analysis identity")
        keys = tuple((item.segment_id, item.receiver_chain_id) for item in self.streams)
        if keys != tuple(sorted(keys, key=lambda item: (str(item[0]), str(item[1])))):
            raise ValueError("surrogate-null streams must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > MAXIMUM_SURROGATE_NULL_STREAMS:
            raise ValueError("surrogate-null streams are duplicate or unbounded")
        if any(
            item.evidence.exact.recording_id != self.recording_id
            or item.evidence.exact.recording_identity_digest
            != self.recording_identity_digest
            for item in self.streams
        ):
            raise ValueError("surrogate-null stream belongs to another recording")
        if self.calibrated_detection_count is not None:
            raise ValueError("surrogate-null output cannot count detections")
        required = {
            "finite-paired-surrogate-controls",
            "not-calibrated-p-values",
            "not-calibrated-detections",
        }
        if self.state is StarlinkSurrogateNullRecordingState.CANDIDATES:
            if not self.streams or not required <= set(self.reason_codes):
                raise ValueError("candidate bundle requires evidence disclosures")
        elif self.streams or self.reason_codes != ("clipped-pilot-band",):
            raise ValueError("not-evaluated bundle must be explicit and empty")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSurrogateNullProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slsnullrec_"):
            raise ValueError("invalid surrogate-null product identity")

    @property
    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.bundle_ref.digest,
            SchemaRef(StarlinkSurrogateNullRecordingBundleV0_1.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkSurrogateNullCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    input_recording_digest: Digest
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    request_digest: Digest
    state: StarlinkSurrogateNullRecordingState
    stream_count: int
    method_count: int
    surrogate_score_count: int

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if (
            isinstance(self.stream_count, bool)
            or not isinstance(self.stream_count, int)
            or self.stream_count < 0
            or self.stream_count > MAXIMUM_SURROGATE_NULL_STREAMS
        ):
            raise ValueError("projection stream count is out of bounds")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.method_count, self.surrogate_score_count)
        ):
            raise ValueError("projection counts must be integers")
        if self.method_count != self.stream_count * len(REPORT_METHOD_ORDER):
            raise ValueError("projection method count is inconsistent")
        if self.stream_count == 0:
            if self.surrogate_score_count != 0:
                raise ValueError("empty projection cannot contain scores")
        elif (
            not self.method_count
            <= self.surrogate_score_count
            <= (self.method_count * MAXIMUM_SURROGATES)
        ):
            raise ValueError("projection surrogate score count is inconsistent")


@dataclass(frozen=True)
class StarlinkSurrogateNullQueryV0_1:
    recording_id: RecordingId
    methods: tuple[StarlinkDetectorMethod, ...] = REPORT_METHOD_ORDER
    radio_ids: tuple[RadioId, ...] = ()
    channel_numbers: tuple[int, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    interval_start_utc_ns: UtcNs | None = None
    interval_stop_utc_ns: UtcNs | None = None
    maximum_rows: int = MAXIMUM_SURROGATE_NULL_QUERY_ROWS

    def __post_init__(self) -> None:
        if not self.methods or len(set(self.methods)) != len(self.methods):
            raise ValueError("query methods must be non-empty and unique")
        if len(set(self.radio_ids)) != len(self.radio_ids):
            raise ValueError("query radios must be unique")
        if any(
            isinstance(value, bool) or value not in (1, 2, 3, 4)
            for value in self.channel_numbers
        ):
            raise ValueError("query channels must be one of 1,2,3,4")
        if len(set(self.channel_numbers)) != len(self.channel_numbers):
            raise ValueError("query channels must be unique")
        if len(set(self.edges)) != len(self.edges):
            raise ValueError("query edges must be unique")
        for value, label in (
            (self.interval_start_utc_ns, "interval_start_utc_ns"),
            (self.interval_stop_utc_ns, "interval_stop_utc_ns"),
        ):
            if value is not None:
                require_utc_ns(value, label)
        if (
            self.interval_start_utc_ns is not None
            and self.interval_stop_utc_ns is not None
            and self.interval_stop_utc_ns <= self.interval_start_utc_ns
        ):
            raise ValueError("query interval must be non-empty")
        if (
            isinstance(self.maximum_rows, bool)
            or not isinstance(self.maximum_rows, int)
            or not 1 <= self.maximum_rows <= MAXIMUM_SURROGATE_NULL_QUERY_ROWS
        ):
            raise ValueError("query maximum_rows is out of bounds")


@dataclass(frozen=True)
class StarlinkSurrogateNullMethodRowV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    method: StarlinkDetectorMethod
    qin_score: float
    surrogate_scores: tuple[float, ...]
    finite_upper_tail_rank: float
    qin_winning_epoch_sample: int
    qin_winning_coarse_cfo_hz: float
    qin_winning_residual_cfo_hz: float
    surrogate_patterns: tuple[StarlinkSearchPatternV0_1, ...]
    provenance: Provenance
    calibrated_p_value: None
    calibrated_detection: None

    def __post_init__(self) -> None:
        if not 1 <= len(self.surrogate_scores) <= MAXIMUM_SURROGATES:
            raise ValueError("method row surrogate count is out of bounds")
        if len(self.surrogate_patterns) != len(self.surrogate_scores):
            raise ValueError("one pattern identity is required per surrogate score")
        for value, label in (
            (self.qin_score, "qin_score"),
            (self.finite_upper_tail_rank, "finite_upper_tail_rank"),
            (self.qin_winning_coarse_cfo_hz, "qin_winning_coarse_cfo_hz"),
            (self.qin_winning_residual_cfo_hz, "qin_winning_residual_cfo_hz"),
        ):
            require_finite(value, label)
        if not 0 <= self.qin_score <= 1 or any(
            not 0 <= value <= 1 for value in self.surrogate_scores
        ):
            raise ValueError("method row scores must lie in [0,1]")
        expected_rank = (
            1 + sum(value >= self.qin_score for value in self.surrogate_scores)
        ) / (len(self.surrogate_scores) + 1)
        if abs(self.finite_upper_tail_rank - expected_rank) > 1e-12:
            raise ValueError("method row finite rank is inconsistent")
        if self.qin_winning_epoch_sample < 0:
            raise ValueError("method row winning epoch must be non-negative")


@dataclass(frozen=True)
class StarlinkSurrogateNullMethodAggregateV0_1:
    method: StarlinkDetectorMethod
    row_count: int
    mean_qin_score: float
    mean_surrogate_score: float
    mean_finite_upper_tail_rank: float
    qin_above_all_surrogates_count: int
    statistic_semantics: str

    def __post_init__(self) -> None:
        if self.row_count <= 0:
            raise ValueError("method aggregate requires rows")
        for value, label in (
            (self.mean_qin_score, "mean_qin_score"),
            (self.mean_surrogate_score, "mean_surrogate_score"),
            (self.mean_finite_upper_tail_rank, "mean_finite_upper_tail_rank"),
        ):
            require_finite(value, label)
            if not 0 <= value <= 1:
                raise ValueError(f"{label} must lie in [0,1]")
        if not 0 <= self.qin_above_all_surrogates_count <= self.row_count:
            raise ValueError("aggregate exceedance count is inconsistent")
        if self.statistic_semantics != (
            "finite-paired-upper-tail-rank-not-calibrated-p-value"
        ):
            raise ValueError("aggregate must disclose finite-rank semantics")


@dataclass(frozen=True)
class RecordingStarlinkSurrogateNullViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    state: StarlinkSurrogateNullRecordingState
    analysis_ref: ArtifactRef
    query: StarlinkSurrogateNullQueryV0_1
    total_matching_rows: int
    rows: tuple[StarlinkSurrogateNullMethodRowV0_1, ...]
    aggregates: tuple[StarlinkSurrogateNullMethodAggregateV0_1, ...]
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.starlink-surrogate-null"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported surrogate-null query view schema")
        if self.recording_id != self.query.recording_id:
            raise ValueError("view and query recording identities differ")
        if self.total_matching_rows < len(self.rows):
            raise ValueError("view total cannot be below returned rows")
        if len(self.rows) > self.query.maximum_rows:
            raise ValueError("view exceeds query row bound")
        methods = tuple(item.method for item in self.aggregates)
        if methods != tuple(
            method for method in REPORT_METHOD_ORDER if method in methods
        ):
            raise ValueError("view aggregates must be in report-method order")
        required = {
            "finite-rank-not-calibrated-p-value",
            "candidate-evidence-not-detection",
        }
        if not required <= set(self.warnings):
            raise ValueError("query view must disclose non-calibrated semantics")


class StarlinkSurrogateNullRecordingAnalyzerV0_1(Protocol):
    def analyze_surrogate_null(
        self,
        recording: RecordingView,
        request: StarlinkSurrogateNullRequestV0_1,
    ) -> StarlinkSurrogateNullRecordingBundleV0_1: ...


class RecordingStarlinkSurrogateNullQueryPortV0_1(Protocol):
    def recording_starlink_surrogate_null(
        self, query: StarlinkSurrogateNullQueryV0_1
    ) -> RecordingStarlinkSurrogateNullViewV0_1: ...
