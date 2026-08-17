"""Calibration and decision contracts for report-method whole-search scores."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_finite, require_positive, require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import (
    StarlinkDetectorMethod,
    StarlinkSearchMode,
)


@dataclass(frozen=True)
class StarlinkSuiteSearchProfileV0_1:
    """Reusable statistical search identity, excluding observation identity."""

    schema: SchemaRef
    method: StarlinkDetectorMethod
    search_mode: StarlinkSearchMode
    selection_method: StarlinkDetectorMethod
    effective_search_cell_count: int
    sample_rate_hz: float
    probe_sample_count: int
    edge: StarlinkEdge
    pilot_symbol_indices: tuple[int, ...]
    symbol_set_role: str
    symbol_split_digest: Digest | None
    control_conditioning: str
    algorithm_digest: Digest
    config_digest: Digest
    exact_template_digest: Digest
    conditioned_control_template_digest: Digest

    SCHEMA_ID = "org.leo-flow.starlink-suite-search-profile"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported suite search-profile schema")
        if self.effective_search_cell_count <= 0:
            raise ValueError("search profile requires positive search cells")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if self.probe_sample_count <= 0:
            raise ValueError("search profile requires a positive probe")
        if not self.pilot_symbol_indices:
            raise ValueError("search profile requires pilot symbols")
        require_token(self.symbol_set_role, "symbol_set_role")
        require_token(self.control_conditioning, "control_conditioning")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSuitePositiveGateV0_1:
    """A declared positive-injection sensitivity gate at one SNR."""

    injection_snr_db: float
    trial_count: int
    minimum_detection_probability: float
    confidence_level: float

    def __post_init__(self) -> None:
        require_finite(self.injection_snr_db, "injection_snr_db")
        if (
            isinstance(self.trial_count, bool)
            or not isinstance(self.trial_count, int)
            or self.trial_count <= 0
        ):
            raise ValueError("positive gate trial_count must be positive")
        for name in ("minimum_detection_probability", "confidence_level"):
            require_positive(getattr(self, name), name)
            if getattr(self, name) >= 1:
                raise ValueError(f"{name} must lie below one")
        if self.confidence_level <= 0.5:
            raise ValueError("positive gate confidence must exceed one half")


@dataclass(frozen=True)
class StarlinkSuiteCalibrationCellPlanV0_1:
    """One non-poolable method/radio/receiver/tuning/search calibration cell."""

    schema: SchemaRef
    cell_id: str
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    method: StarlinkDetectorMethod
    hardware_profile_digest: Digest
    tuning_identity_digest: Digest
    algorithm_digest: Digest
    config_digest: Digest
    exact_template_digest: Digest
    conditioned_control_template_digest: Digest
    search_identity_digest: Digest
    statistic: str
    threshold_comparison: str
    target_whole_search_far: float
    null_confidence_level: float
    training_null_search_count: int
    holdout_null_search_count: int
    positive_gates: tuple[StarlinkSuitePositiveGateV0_1, ...]

    SCHEMA_ID = "org.leo-flow.starlink-suite-calibration-cell-plan"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported suite calibration cell-plan schema")
        require_token(self.cell_id, "cell_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("channel_number must be one of 1, 2, 3, 4")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz < 1_875_000:
            raise ValueError("clipped pilot-band captures cannot be calibrated")
        if (
            isinstance(self.probe_sample_count, bool)
            or not isinstance(self.probe_sample_count, int)
            or self.probe_sample_count <= 0
        ):
            raise ValueError("probe_sample_count must be positive")
        if self.statistic != "whole-search-reported-score":
            raise ValueError("unsupported suite calibration statistic")
        if self.threshold_comparison != "strict-greater-than":
            raise ValueError("report-compatible decisions require score > threshold")
        require_positive(self.target_whole_search_far, "target_whole_search_far")
        if self.target_whole_search_far >= 1:
            raise ValueError("target whole-search FAR must lie below one")
        require_positive(self.null_confidence_level, "null_confidence_level")
        if not 0.5 < self.null_confidence_level < 1:
            raise ValueError("null confidence level must lie in (0.5, 1)")
        for name in (
            "training_null_search_count",
            "holdout_null_search_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.positive_gates:
            raise ValueError("suite calibration requires positive-injection gates")
        snrs = tuple(item.injection_snr_db for item in self.positive_gates)
        if tuple(sorted(snrs)) != snrs or len(set(snrs)) != len(snrs):
            raise ValueError("positive gates must use sorted unique SNRs")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSuitePositivePerformanceV0_1:
    injection_snr_db: float
    trial_count: int
    detection_count: int
    detection_probability_lower_bound: float
    confidence_level: float
    minimum_detection_probability: float
    accepted: bool

    def __post_init__(self) -> None:
        require_finite(self.injection_snr_db, "injection_snr_db")
        if self.trial_count <= 0 or not 0 <= self.detection_count <= self.trial_count:
            raise ValueError("positive performance counts are invalid")
        for name in (
            "detection_probability_lower_bound",
            "confidence_level",
            "minimum_detection_probability",
        ):
            require_finite(getattr(self, name), name)
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0, 1]")
        expected = (
            self.detection_probability_lower_bound >= self.minimum_detection_probability
        )
        if self.accepted is not expected:
            raise ValueError("positive performance acceptance is inconsistent")


@dataclass(frozen=True)
class StarlinkSuiteCalibrationEvidenceV0_1:
    """Frozen threshold evidence with disjoint null and positive gates."""

    schema: SchemaRef
    evidence_id: str
    cell_plan_digest: Digest
    corpus_digest: Digest
    threshold: float
    threshold_comparison: str
    training_null_search_count: int
    training_threshold_exceedance_count: int
    holdout_null_search_count: int
    holdout_threshold_exceedance_count: int
    holdout_far_upper_bound: float
    target_whole_search_far: float
    null_confidence_level: float
    positive_performance: tuple[StarlinkSuitePositivePerformanceV0_1, ...]
    accepted: bool

    SCHEMA_ID = "org.leo-flow.starlink-suite-calibration-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported suite calibration evidence schema")
        require_token(self.evidence_id, "evidence_id")
        require_finite(self.threshold, "threshold")
        if self.threshold_comparison != "strict-greater-than":
            raise ValueError("unsupported threshold comparison")
        for count, total, label in (
            (
                self.training_threshold_exceedance_count,
                self.training_null_search_count,
                "training",
            ),
            (
                self.holdout_threshold_exceedance_count,
                self.holdout_null_search_count,
                "holdout",
            ),
        ):
            if total <= 0 or not 0 <= count <= total:
                raise ValueError(f"{label} exceedance count is invalid")
        for name in (
            "holdout_far_upper_bound",
            "target_whole_search_far",
            "null_confidence_level",
        ):
            require_finite(getattr(self, name), name)
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0, 1]")
        if not self.positive_performance:
            raise ValueError("suite calibration evidence requires positive results")
        expected = self.holdout_far_upper_bound <= self.target_whole_search_far and all(
            item.accepted for item in self.positive_performance
        )
        if self.accepted is not expected:
            raise ValueError("suite calibration acceptance is inconsistent")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ApprovedStarlinkSuiteCalibrationV0_1:
    """Deployable threshold for exactly one report-method statistical cell."""

    schema: SchemaRef
    calibration_id: str
    cell_plan: StarlinkSuiteCalibrationCellPlanV0_1
    evidence_ref: ArtifactRef
    corpus_digest: Digest
    threshold: float

    SCHEMA_ID = "org.leo-flow.approved-starlink-suite-calibration"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported approved suite calibration schema")
        require_token(self.calibration_id, "calibration_id")
        require_finite(self.threshold, "threshold")
        if self.evidence_ref.schema != SchemaRef(
            StarlinkSuiteCalibrationEvidenceV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("approved calibration must cite suite evidence")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.calibration_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkSuiteMethodDecisionV0_1:
    """One calibrated method decision; it is not a beacon or event count."""

    schema: SchemaRef
    suite_ref: ArtifactRef
    calibration_ref: ArtifactRef
    method: StarlinkDetectorMethod
    score: float
    threshold: float
    threshold_comparison: str
    detected: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-suite-method-decision"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported suite method-decision schema")
        require_finite(self.score, "score")
        require_finite(self.threshold, "threshold")
        if self.threshold_comparison != "strict-greater-than":
            raise ValueError("unsupported method-decision comparison")
        if self.detected is not (self.score > self.threshold):
            raise ValueError("method decision disagrees with its threshold")
        if "method-decision-not-beacon-count" not in self.reason_codes:
            raise ValueError("method decisions must forbid direct beacon counting")
