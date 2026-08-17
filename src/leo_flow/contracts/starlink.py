"""Versioned, non-decisional Starlink known-code analysis contracts.

The search maximum is a candidate, never a detection. A separate calibrated
evaluation contract is the only place that may carry a detection boolean.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_positive, require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)


class StarlinkEdge(str, Enum):
    LOWER = "lower"
    UPPER = "upper"


class StarlinkEvaluationState(str, Enum):
    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"


class StarlinkRecordingDecisionState(str, Enum):
    NOT_EVALUATED = "not_evaluated"
    CANDIDATES = "candidates"
    CALIBRATED_DETECTIONS = "calibrated_detections"


@dataclass(frozen=True)
class StarlinkPilotSearchCandidateV0_1:
    """One receiver's maximum over an explicitly identified search."""

    schema: SchemaRef
    candidate_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    pilot_indices: tuple[int, ...]
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    search_identity_digest: Digest
    sample_rate_hz: float
    probe_sample_count: int
    frame_period_samples: float
    epoch_hypotheses_samples: tuple[int, ...]
    cfo_hypotheses_hz: tuple[float, ...]
    search_cell_count: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    searched_exact_score: float
    conditioned_exact_score: float
    conditioned_control_score: float
    exact_minus_control_margin: float
    frame_support: int
    control_conditioning: str
    pss_evidence_status: str
    provenance: Provenance
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pilot-search-candidate"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink pilot candidate schema")
        require_token(self.candidate_id, "candidate_id")
        allowed = (
            set(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else set(range(488, 496))
        )
        if (
            not self.pilot_indices
            or tuple(sorted(set(self.pilot_indices))) != self.pilot_indices
            or not set(self.pilot_indices) <= allowed
        ):
            raise ValueError("pilot indices must be a sorted subset of the edge")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if (
            isinstance(self.probe_sample_count, bool)
            or not isinstance(self.probe_sample_count, int)
            or self.probe_sample_count <= 0
        ):
            raise ValueError("probe_sample_count must be a positive integer")
        require_positive(self.frame_period_samples, "frame_period_samples")
        if (
            not self.epoch_hypotheses_samples
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.epoch_hypotheses_samples
            )
            or len(set(self.epoch_hypotheses_samples))
            != len(self.epoch_hypotheses_samples)
        ):
            raise ValueError("epoch hypotheses must be unique non-negative integers")
        if not self.cfo_hypotheses_hz or len(set(self.cfo_hypotheses_hz)) != len(
            self.cfo_hypotheses_hz
        ):
            raise ValueError("CFO hypotheses must be non-empty and unique")
        for value in self.cfo_hypotheses_hz:
            require_finite(value, "cfo hypothesis")
        if self.search_cell_count != len(self.epoch_hypotheses_samples) * len(
            self.cfo_hypotheses_hz
        ):
            raise ValueError("search cell count must describe the declared bank")
        if self.winning_epoch_sample not in self.epoch_hypotheses_samples:
            raise ValueError("winning epoch is outside the declared search")
        if self.winning_cfo_hz not in self.cfo_hypotheses_hz:
            raise ValueError("winning CFO is outside the declared search")
        for name in (
            "searched_exact_score",
            "conditioned_exact_score",
            "conditioned_control_score",
            "exact_minus_control_margin",
        ):
            require_finite(getattr(self, name), name)
        if self.frame_support <= 0:
            raise ValueError("candidate must be supported by at least one frame")
        if self.control_conditioning != "winning-epoch-and-cfo-fixed":
            raise ValueError("control must be conditioned at the winning search cell")
        if self.pss_evidence_status not in ("not_evaluated", "evaluated"):
            raise ValueError("PSS evidence status is invalid")
        if abs(self.searched_exact_score - self.conditioned_exact_score) > 1e-12:
            raise ValueError(
                "the winning searched score must reproduce when conditioned"
            )
        expected_margin = self.conditioned_exact_score - self.conditioned_control_score
        if abs(self.exact_minus_control_margin - expected_margin) > 1e-12:
            raise ValueError("exact/control margin is inconsistent")


@dataclass(frozen=True)
class StarlinkPilotAnalysisBundleV0_1:
    """Bounded candidates for one immutable recording, without verdict bits."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    candidates: tuple[StarlinkPilotSearchCandidateV0_1, ...]
    warnings: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.starlink-pilot-analysis-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink pilot analysis bundle schema")
        require_token(self.analysis_id, "analysis_id")
        if any(
            item.recording_id != self.recording_id
            or item.recording_identity_digest != self.recording_identity_digest
            for item in self.candidates
        ):
            raise ValueError(
                "analysis bundle contains a candidate from another recording"
            )
        identities = [item.candidate_id for item in self.candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("analysis bundle contains duplicate candidates")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkPilotCalibrationV0_1:
    """Threshold measured for one exact whole-search statistic and null corpus."""

    schema: SchemaRef
    calibration_id: str
    algorithm_digest: Digest
    config_digest: Digest
    exact_template_digest: Digest
    conditioned_control_template_digest: Digest
    search_identity_digest: Digest
    hardware_profile_digest: Digest
    null_dataset_digest: Digest
    null_split_digest: Digest
    statistic: str
    threshold_scope: str
    threshold: float
    target_false_alarm_rate: float
    null_search_count: int
    threshold_exceedance_count: int

    SCHEMA_ID = "org.leo-flow.starlink-pilot-calibration"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink pilot calibration schema")
        require_token(self.calibration_id, "calibration_id")
        if self.statistic != "searched-exact-minus-conditioned-control-margin":
            raise ValueError("unsupported Starlink pilot decision statistic")
        if self.threshold_scope != "whole-search":
            raise ValueError("threshold must be calibrated after search maximization")
        require_finite(self.threshold, "threshold")
        require_positive(self.target_false_alarm_rate, "target_false_alarm_rate")
        if self.target_false_alarm_rate >= 1:
            raise ValueError("target false alarm rate must lie below one")
        if self.null_search_count <= 0:
            raise ValueError("calibration requires null whole-search trials")
        if not 0 <= self.threshold_exceedance_count <= self.null_search_count:
            raise ValueError("threshold exceedance count is outside the null corpus")

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.calibration_id,
            canonical_digest(self),
            SchemaRef(self.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkPilotEvaluationV0_1:
    """A candidate evaluation; uncalibrated results cannot say detected."""

    schema: SchemaRef
    candidate_id: str
    candidate_digest: Digest
    state: StarlinkEvaluationState
    statistic: str
    score: float
    calibration_ref: ArtifactRef | None
    threshold: float | None
    detected: bool | None
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pilot-evaluation"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink pilot evaluation schema")
        require_token(self.candidate_id, "candidate_id")
        require_finite(self.score, "score")
        if self.state is StarlinkEvaluationState.UNCALIBRATED:
            if self.calibration_ref is not None or self.threshold is not None:
                raise ValueError("uncalibrated evaluation cannot cite a threshold")
            if self.detected is not None:
                raise ValueError("uncalibrated evaluation cannot emit a detection bit")
        else:
            if self.calibration_ref is None or self.threshold is None:
                raise ValueError("calibrated evaluation must cite its threshold")
            require_finite(self.threshold, "threshold")
            if self.detected is None:
                raise ValueError("calibrated evaluation must emit a detection bit")


@dataclass(frozen=True)
class RecordingStarlinkDecisionViewV0_1:
    """Dashboard summary that cannot turn search rows into detection counts."""

    schema: SchemaRef
    recording_id: RecordingId
    state: StarlinkRecordingDecisionState
    analyzed_stream_count: int
    search_candidate_count: int
    calibrated_detection_count: int | None
    analysis_ref: ArtifactRef | None
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-decision"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording Starlink decision view schema")
        for name in ("analyzed_stream_count", "search_candidate_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.state is StarlinkRecordingDecisionState.NOT_EVALUATED:
            if self.analysis_ref is not None or self.analyzed_stream_count != 0:
                raise ValueError("not-evaluated view cannot cite analysis")
            if self.search_candidate_count != 0:
                raise ValueError("not-evaluated view cannot contain candidates")
        elif self.analysis_ref is None or self.analyzed_stream_count == 0:
            raise ValueError("evaluated view must cite non-empty analysis")
        if self.state is StarlinkRecordingDecisionState.CALIBRATED_DETECTIONS:
            if self.calibrated_detection_count is None:
                raise ValueError("calibrated view must carry a detection count")
            if not 0 <= self.calibrated_detection_count <= self.search_candidate_count:
                raise ValueError("calibrated detection count is outside candidates")
        elif self.calibrated_detection_count is not None:
            raise ValueError("uncalibrated dashboard state cannot count detections")
