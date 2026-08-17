"""Durable v0.2 contracts for the complete Starlink report detector suite."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView

from ._validation import require_finite, require_token
from .core import (
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    V0_2,
    StarlinkDetectorMethod,
    StarlinkDetectorSuiteBundleV0_2,
)
from .storage import ObjectRef, RecordingObjectRef


class StarlinkSuiteRecordingState(str, Enum):
    CANDIDATES = "candidates"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class StarlinkSuiteStreamSelectionV0_2:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    probe_sample_count: int

    def __post_init__(self) -> None:
        expected = SchemaRef("org.leo-flow.starlink-edge-pilot-template")
        if (
            self.exact_template_ref.schema != expected
            or self.conditioned_control_template_ref.schema != expected
            or self.exact_template_ref == self.conditioned_control_template_ref
        ):
            raise ValueError("suite selection requires distinct exact v0.1 templates")
        if isinstance(self.probe_sample_count, bool) or self.probe_sample_count <= 0:
            raise ValueError("suite probe_sample_count must be positive")


@dataclass(frozen=True)
class StarlinkDetectorSuiteRequestV0_2:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    stream_selections: tuple[StarlinkSuiteStreamSelectionV0_2, ...]
    requested_output_schema: SchemaRef
    ineligible_reason: str | None = None

    SCHEMA_ID = "org.leo-flow.starlink-detector-suite-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported detector-suite request")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("suite request recording identities differ")
        if self.requested_output_schema != SchemaRef(
            StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2
        ):
            raise ValueError("unsupported detector-suite output schema")
        keys = tuple(
            (item.segment_id, item.receiver_chain_id) for item in self.stream_selections
        )
        if keys != tuple(
            sorted(keys, key=lambda value: (str(value[0]), str(value[1])))
        ):
            raise ValueError("suite stream selections must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > 64:
            raise ValueError("suite stream selections are duplicate or unbounded")
        if self.ineligible_reason is None:
            if not self.stream_selections:
                raise ValueError("eligible suite request requires streams")
        elif self.ineligible_reason != "clipped-pilot-band":
            raise ValueError("unknown suite ineligibility reason")
        elif self.stream_selections:
            raise ValueError("ineligible suite request cannot select streams")


@dataclass(frozen=True)
class StarlinkDetectorSuiteRecordingBundleV0_2:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    state: StarlinkSuiteRecordingState
    suites: tuple[StarlinkDetectorSuiteBundleV0_2, ...]
    reason_codes: tuple[str, ...]
    calibrated_detection_count: int | None

    SCHEMA_ID = "org.leo-flow.starlink-detector-suite-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported detector-suite recording bundle")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slsuite_"):
            raise ValueError("invalid detector-suite analysis identity")
        keys = tuple((item.segment_id, item.receiver_chain_id) for item in self.suites)
        if keys != tuple(
            sorted(keys, key=lambda value: (str(value[0]), str(value[1])))
        ):
            raise ValueError("suite bundle streams must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > 64:
            raise ValueError("suite bundle streams are duplicate or unbounded")
        if any(
            item.recording_id != self.recording_id
            or item.recording_identity_digest != self.recording_identity_digest
            for item in self.suites
        ):
            raise ValueError("stream suite belongs to another recording")
        if self.calibrated_detection_count is not None:
            raise ValueError("v0.2 suite output is candidate-only")
        if self.state is StarlinkSuiteRecordingState.CANDIDATES:
            if (
                not self.suites
                or "whole-search-calibration-required" not in self.reason_codes
            ):
                raise ValueError(
                    "candidate suite requires streams and calibration warning"
                )
        elif self.suites or self.reason_codes != ("clipped-pilot-band",):
            raise ValueError("not-evaluated suite must be explicit and empty")

    @property
    def digest(self) -> Digest:
        from .core import canonical_digest

        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkDetectorSuiteProductRefV0_2:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slsuite_"):
            raise ValueError("invalid detector-suite product identity")


@dataclass(frozen=True)
class StarlinkMethodComparisonV0_2:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    method: StarlinkDetectorMethod
    score: float
    control_score: float
    margin: float
    epoch_sample: int
    coarse_cfo_hz: float
    residual_cfo_hz: float
    effective_search_cell_count: int
    frame_support: int

    def __post_init__(self) -> None:
        for name in (
            "score",
            "control_score",
            "margin",
            "coarse_cfo_hz",
            "residual_cfo_hz",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.epoch_sample < 0
            or self.effective_search_cell_count <= 0
            or self.frame_support <= 0
        ):
            raise ValueError("method comparison bounds are invalid")


@dataclass(frozen=True)
class RecordingStarlinkSuiteViewV0_2:
    schema: SchemaRef
    recording_id: RecordingId
    state: StarlinkSuiteRecordingState
    analysis_ref: ArtifactRef
    analyzed_stream_count: int
    method_count: int
    calibrated_detection_count: int | None
    reason_codes: tuple[str, ...]
    methods: tuple[StarlinkMethodComparisonV0_2, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-detector-suite"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported detector-suite dashboard view")
        if self.calibrated_detection_count is not None:
            raise ValueError("uncalibrated detector suite cannot count detections")
        if self.method_count != len(self.methods):
            raise ValueError("dashboard method count differs")
        if self.state is StarlinkSuiteRecordingState.CANDIDATES:
            if self.analyzed_stream_count <= 0:
                raise ValueError("candidate dashboard requires analyzed streams")
            if self.method_count != self.analyzed_stream_count * len(
                REPORT_METHOD_ORDER
            ):
                raise ValueError("dashboard must contain every report method")
            if "whole-search-calibration-required" not in self.reason_codes:
                raise ValueError("dashboard must disclose calibration requirement")
        elif self.analyzed_stream_count or self.method_count or self.methods:
            raise ValueError("not-evaluated dashboard view must be empty")


class StarlinkDetectorSuiteRecordingAnalyzerV0_2(Protocol):
    def analyze_starlink_suite(
        self, recording: RecordingView, request: StarlinkDetectorSuiteRequestV0_2
    ) -> StarlinkDetectorSuiteRecordingBundleV0_2: ...


class RecordingStarlinkSuiteQueryPortV0_2(Protocol):
    def recording_starlink_suite(
        self, recording_id: RecordingId
    ) -> RecordingStarlinkSuiteViewV0_2: ...
