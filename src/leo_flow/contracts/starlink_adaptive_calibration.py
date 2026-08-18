"""Offline calibration contracts for adaptive Starlink/QAM evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_token
from .core import V0_1, Digest, RadioId, ReceiverChainId, SchemaRef, canonical_digest

MAXIMUM_CALIBRATION_PATTERNS = 33
MAXIMUM_CALIBRATION_RECEIVERS = 8


class AdaptiveCalibrationSplit(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    LOCKED_TEST = "locked-test"


class AdaptiveCalibrationLabel(str, Enum):
    NULL = "null"
    POSITIVE = "positive"


class AdaptivePatternRole(str, Enum):
    QIN = "qin"
    SURROGATE = "surrogate"


@dataclass(frozen=True)
class AdaptiveReceiverPatternEvidenceV0_1:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    whole_search_maximum: float
    candidate_count: int
    temporal_maximum: float
    qam_goodness: float
    qam_complete_frame_count: int

    def __post_init__(self) -> None:
        for name in ("whole_search_maximum", "temporal_maximum", "qam_goodness"):
            require_finite(getattr(self, name), name)
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0,1]")
        if self.candidate_count < 0 or self.qam_complete_frame_count < 0:
            raise ValueError("adaptive evidence counts cannot be negative")
        if self.candidate_count == 0 and self.whole_search_maximum != 0:
            raise ValueError("zero-candidate evidence must retain a zero maximum")
        if self.qam_complete_frame_count == 0 and self.qam_goodness != 0:
            raise ValueError("QAM goodness requires complete frames")

    @property
    def identity(self) -> tuple[str, str]:
        return str(self.radio_id), str(self.receiver_chain_id)


@dataclass(frozen=True)
class AdaptivePatternDwellEvidenceV0_1:
    pattern_index: int
    role: AdaptivePatternRole
    receiver_evidence: tuple[AdaptiveReceiverPatternEvidenceV0_1, ...]
    dual_rx_coherent_qam: bool

    def __post_init__(self) -> None:
        expected_role = (
            AdaptivePatternRole.QIN
            if self.pattern_index == 0
            else AdaptivePatternRole.SURROGATE
        )
        identities = tuple(item.identity for item in self.receiver_evidence)
        if (
            not 0 <= self.pattern_index < MAXIMUM_CALIBRATION_PATTERNS
            or self.role is not expected_role
            or not identities
            or len(identities) > MAXIMUM_CALIBRATION_RECEIVERS
            or identities != tuple(sorted(set(identities)))
        ):
            raise ValueError("adaptive pattern evidence is not canonical")
        if (
            self.dual_rx_coherent_qam
            and sum(
                item.qam_complete_frame_count > 0 for item in self.receiver_evidence
            )
            < 2
        ):
            raise ValueError("coherent dual-RX QAM requires two receiver fits")

    @property
    def whole_search_maximum(self) -> float:
        return max(item.whole_search_maximum for item in self.receiver_evidence)


@dataclass(frozen=True)
class AdaptiveCalibrationDwellV0_1:
    dwell_id: str
    member_digest: Digest
    group_digest: Digest
    split: AdaptiveCalibrationSplit
    label: AdaptiveCalibrationLabel
    cell_identity_digest: Digest
    patterns: tuple[AdaptivePatternDwellEvidenceV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.dwell_id, "dwell_id")
        indices = tuple(item.pattern_index for item in self.patterns)
        if not indices or indices != tuple(range(len(indices))):
            raise ValueError("every dwell must retain Qin and every surrogate")
        receiver_shapes = tuple(
            tuple(item.identity for item in pattern.receiver_evidence)
            for pattern in self.patterns
        )
        if len(set(receiver_shapes)) != 1:
            raise ValueError("Qin and surrogates require symmetric receiver evidence")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class AdaptiveCalibrationPlanV0_1:
    schema: SchemaRef
    plan_id: str
    cell_identity_digest: Digest
    pattern_count: int
    train_manifest_digest: Digest
    validation_manifest_digest: Digest
    locked_test_manifest_digest: Digest
    training_null_dwell_count: int
    validation_null_dwell_count: int
    validation_positive_dwell_count: int
    locked_test_null_dwell_count: int
    locked_test_positive_dwell_count: int
    target_family_wise_false_alarm_rate: float
    minimum_positive_detection_probability: float
    confidence_level: float
    minimum_temporal_maximum: float
    minimum_qam_goodness_per_receiver: float
    minimum_coherent_qam_receiver_count: int
    statistic: str = "maximum-over-time-cfo-epoch-receiver-and-pattern-per-dwell"
    threshold_comparison: str = "strict-greater-than"
    threshold_selection: str = "descending-order-statistic-at-floor-n-times-target-far"

    SCHEMA_ID = "org.leo-flow.adaptive-starlink-calibration-plan"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive calibration plan schema")
        require_token(self.plan_id, "plan_id")
        if not 1 <= self.pattern_count <= MAXIMUM_CALIBRATION_PATTERNS:
            raise ValueError("adaptive calibration pattern count is invalid")
        for name in (
            "training_null_dwell_count",
            "validation_null_dwell_count",
            "validation_positive_dwell_count",
            "locked_test_null_dwell_count",
            "locked_test_positive_dwell_count",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "target_family_wise_false_alarm_rate",
            "minimum_positive_detection_probability",
            "confidence_level",
        ):
            require_finite(getattr(self, name), name)
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must lie in (0,1)")
        if self.confidence_level <= 0.5:
            raise ValueError("adaptive calibration confidence must exceed one half")
        for name in ("minimum_temporal_maximum", "minimum_qam_goodness_per_receiver"):
            require_finite(getattr(self, name), name)
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0,1]")
        if (
            not 0
            <= self.minimum_coherent_qam_receiver_count
            <= MAXIMUM_CALIBRATION_RECEIVERS
        ):
            raise ValueError("coherent QAM receiver gate is invalid")
        if (
            self.minimum_qam_goodness_per_receiver > 0
            and self.minimum_coherent_qam_receiver_count < 2
        ):
            raise ValueError("QAM gating requires coherent dual-RX evidence")
        if (
            self.statistic
            != "maximum-over-time-cfo-epoch-receiver-and-pattern-per-dwell"
        ):
            raise ValueError(
                "adaptive calibration must include every look-elsewhere axis"
            )
        if self.threshold_comparison != "strict-greater-than":
            raise ValueError("adaptive calibration uses strict thresholds")
        if self.threshold_selection != (
            "descending-order-statistic-at-floor-n-times-target-far"
        ):
            raise ValueError("adaptive calibration order statistic is unsupported")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class FittedAdaptiveCalibrationV0_1:
    schema: SchemaRef
    fit_id: str
    plan_digest: Digest
    train_manifest_digest: Digest
    threshold: float
    threshold_descending_rank: int
    minimum_resolvable_family_wise_far: float
    training_dwell_count: int
    training_threshold_exceedance_count: int
    training_member_digests: tuple[Digest, ...]
    training_group_digests: tuple[Digest, ...]
    candidate_only: bool

    SCHEMA_ID = "org.leo-flow.fitted-adaptive-starlink-calibration"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1) or not self.fit_id.startswith(
            "sladfit_"
        ):
            raise ValueError("invalid fitted adaptive calibration")
        require_finite(self.threshold, "threshold")
        if (
            not 0 <= self.threshold <= 1
            or self.threshold_descending_rank <= 0
            or self.threshold_descending_rank > self.training_dwell_count
            or self.training_dwell_count <= 0
            or not 0
            <= self.training_threshold_exceedance_count
            <= self.training_dwell_count
            or self.training_threshold_exceedance_count
            >= self.threshold_descending_rank
            or self.minimum_resolvable_family_wise_far != 1 / self.training_dwell_count
            or len(self.training_member_digests) != self.training_dwell_count
            or len(self.training_group_digests) != self.training_dwell_count
            or self.training_member_digests
            != tuple(sorted(set(self.training_member_digests), key=str))
            or self.training_group_digests
            != tuple(sorted(set(self.training_group_digests), key=str))
        ):
            raise ValueError("invalid training calibration counts")
        if not self.candidate_only:
            raise ValueError("training fit cannot authorize detections")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class AdaptiveCalibrationSplitEvidenceV0_1:
    split: AdaptiveCalibrationSplit
    manifest_digest: Digest
    threshold: float
    null_dwell_count: int
    null_family_wise_exceedance_count: int
    null_far_upper_bound: float
    positive_dwell_count: int
    positive_detection_count: int
    positive_detection_probability_lower_bound: float
    member_digests: tuple[Digest, ...]
    group_digests: tuple[Digest, ...]
    accepted: bool

    def __post_init__(self) -> None:
        if self.split is AdaptiveCalibrationSplit.TRAIN:
            raise ValueError("held-out evidence cannot use the training split")
        for count, total in (
            (self.null_family_wise_exceedance_count, self.null_dwell_count),
            (self.positive_detection_count, self.positive_dwell_count),
        ):
            if total <= 0 or not 0 <= count <= total:
                raise ValueError("adaptive split evidence counts are invalid")
        for value in (
            self.threshold,
            self.null_far_upper_bound,
            self.positive_detection_probability_lower_bound,
        ):
            require_finite(value, "adaptive split evidence value")
            if not 0 <= value <= 1:
                raise ValueError("adaptive split evidence values must lie in [0,1]")
        if (
            len(self.member_digests)
            != self.null_dwell_count + self.positive_dwell_count
            or self.member_digests != tuple(sorted(set(self.member_digests), key=str))
            or len(self.group_digests)
            != self.null_dwell_count + self.positive_dwell_count
            or self.group_digests != tuple(sorted(set(self.group_digests), key=str))
        ):
            raise ValueError("adaptive split member inventory is invalid")


@dataclass(frozen=True)
class LockedAdaptiveCalibrationEvidenceV0_1:
    schema: SchemaRef
    evidence_id: str
    plan_digest: Digest
    fit_digest: Digest
    validation: AdaptiveCalibrationSplitEvidenceV0_1
    locked_test: AdaptiveCalibrationSplitEvidenceV0_1
    calibrated_decision_eligible: bool
    candidate_only: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.locked-adaptive-starlink-calibration-evidence"

    def __post_init__(self) -> None:
        required = {
            "whole-search-family-wise-maxima",
            "qin-and-surrogates-treated-symmetrically-under-null",
            "zero-candidate-null-dwells-retained",
            "locked-test-must-be-evaluated-once",
            "binomial-confidence-bounds-not-p-values",
        }
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or not self.evidence_id.startswith("sladlocked_")
            or self.validation.split is not AdaptiveCalibrationSplit.VALIDATION
            or self.locked_test.split is not AdaptiveCalibrationSplit.LOCKED_TEST
            or self.calibrated_decision_eligible
            is not (self.validation.accepted and self.locked_test.accepted)
            or self.candidate_only is self.calibrated_decision_eligible
            or not required <= set(self.warnings)
        ):
            raise ValueError("invalid locked adaptive calibration evidence")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
