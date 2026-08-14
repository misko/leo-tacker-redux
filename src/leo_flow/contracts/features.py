"""Independent-recording analysis request and result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._validation import (
    require_finite,
    require_nonnegative,
    require_token,
    require_utc_ns,
)
from .core import (
    V0_1,
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverChainId,
    ReceiverPairId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from .storage import ObjectRef, RecordingObjectRef


@dataclass(frozen=True)
class Covariance:
    basis: tuple[str, ...]
    units: tuple[str, ...]
    values: tuple[tuple[float, ...], ...]
    psd_tolerance: float = 1e-10

    def __post_init__(self) -> None:
        size = len(self.basis)
        if size == 0 or len(set(self.basis)) != size or len(self.units) != size:
            raise ValueError("covariance basis and units must be aligned and unique")
        if len(self.values) != size or any(len(row) != size for row in self.values):
            raise ValueError("covariance shape does not match basis")
        require_nonnegative(self.psd_tolerance, "psd_tolerance")
        for row in self.values:
            for value in row:
                require_finite(value, "covariance value")
        for i in range(size):
            for j in range(i + 1, size):
                scale = max(1.0, abs(self.values[i][j]), abs(self.values[j][i]))
                if (
                    abs(self.values[i][j] - self.values[j][i])
                    > self.psd_tolerance * scale
                ):
                    raise ValueError("covariance must be symmetric")
        # Dependency-free positive-semidefinite LDL^T test with tolerance.
        lower = [[0.0] * size for _ in range(size)]
        diagonal = [0.0] * size
        for i in range(size):
            lower[i][i] = 1.0
            for j in range(i):
                residual = self.values[i][j] - sum(
                    lower[i][k] * diagonal[k] * lower[j][k] for k in range(j)
                )
                if abs(diagonal[j]) <= self.psd_tolerance:
                    if abs(residual) > self.psd_tolerance:
                        raise ValueError("covariance is not positive semidefinite")
                    lower[i][j] = 0.0
                else:
                    lower[i][j] = residual / diagonal[j]
            pivot = self.values[i][i] - sum(
                lower[i][k] ** 2 * diagonal[k] for k in range(i)
            )
            if pivot < -self.psd_tolerance:
                raise ValueError("covariance is not positive semidefinite")
            diagonal[i] = max(0.0, pivot)


@dataclass(frozen=True)
class FeatureObservation:
    feature_id: FeatureId
    recording_id: RecordingId
    segment_id: SegmentId
    method_id: str
    method_version: str
    window_start_sample: int
    window_stop_sample: int
    segment_sample_count: int
    midpoint_utc_ns: UtcNs
    feature_kind: str
    score: float
    score_semantics: str
    receiver_chain_id: ReceiverChainId | None = None
    receiver_pair_id: ReceiverPairId | None = None
    frequency_hz: float | None = None
    frequency_offset_hz: float | None = None
    drift_hz_s: float | None = None
    noise_estimate: float | None = None
    snr_db: float | None = None
    covariance: Covariance | None = None
    uncertainty: tuple[tuple[str, Any], ...] = ()
    quality_flags: tuple[str, ...] = ()
    diagnostics: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        require_token(self.method_version, "method_version")
        require_token(self.feature_kind, "feature_kind")
        require_token(self.score_semantics, "score_semantics")
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        if (self.receiver_chain_id is None) == (self.receiver_pair_id is None):
            raise ValueError("exactly one receiver chain or receiver pair is required")
        if (
            not 0
            <= self.window_start_sample
            < self.window_stop_sample
            <= self.segment_sample_count
        ):
            raise ValueError("feature window lies outside its segment")
        for name in (
            "score",
            "frequency_hz",
            "frequency_offset_hz",
            "drift_hz_s",
            "noise_estimate",
            "snr_db",
        ):
            value = getattr(self, name)
            if value is not None:
                require_finite(value, name)


@dataclass(frozen=True)
class MethodScore:
    method_id: str
    method_version: str
    segment_id: SegmentId
    receiver_key: str
    window_start_sample: int
    window_stop_sample: int
    score: float
    score_semantics: str

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        require_token(self.method_version, "method_version")
        require_token(self.receiver_key, "receiver_key")
        require_token(self.score_semantics, "score_semantics")
        require_finite(self.score, "score")
        if not 0 <= self.window_start_sample < self.window_stop_sample:
            raise ValueError("score window must be non-empty")


@dataclass(frozen=True)
class RecordingAnalysisRequest:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.recording-analysis-request"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported recording analysis request")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("analysis request recording IDs differ")


@dataclass(frozen=True)
class FeatureSetBundle:
    schema: SchemaRef
    feature_set_id: FeatureSetId
    analysis_run_id: AnalysisRunId
    recording_id: RecordingId
    input_recording_identity_digest: Digest
    provenance: Provenance
    observations: tuple[FeatureObservation, ...]
    method_scores: tuple[MethodScore, ...]
    diagnostic_bundle_ref: ObjectRef | None = None
    warnings: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.feature-set-bundle"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported feature set schema")
        if any(
            observation.recording_id != self.recording_id
            for observation in self.observations
        ):
            raise ValueError("feature set contains another recording")
        keys = [
            (
                s.method_id,
                s.method_version,
                s.segment_id,
                s.receiver_key,
                s.window_start_sample,
                s.window_stop_sample,
            )
            for s in self.method_scores
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("method score shared-window keys must be unique")


@dataclass(frozen=True)
class FeatureSetRef:
    feature_set_id: FeatureSetId
    analysis_run_id: AnalysisRunId
    bundle_ref: ObjectRef


@dataclass(frozen=True)
class DecisionRuleRef:
    rule_id: str
    rule_digest: Digest
    calibration_dataset_id: str
