"""Exact corpus and evidence contracts for Starlink pilot calibration v0.1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_positive, require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    canonical_digest,
)
from .starlink import StarlinkEdge


class StarlinkCalibrationCorpusRole(str, Enum):
    TRAIN_NULL = "train_null"
    HOLDOUT_NULL = "holdout_null"
    POSITIVE_INJECTION = "positive_injection"


@dataclass(frozen=True)
class StarlinkCalibrationCellPlanV0_1:
    """One statistically indivisible radio/receiver/tuning/search stratum."""

    schema: SchemaRef
    cell_id: str
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    hardware_profile_digest: Digest
    tuning_identity_digest: Digest
    channel_number: int
    edge: StarlinkEdge
    algorithm_digest: Digest
    config_digest: Digest
    exact_template_digest: Digest
    conditioned_control_template_digest: Digest
    search_identity_digest: Digest
    target_whole_search_far: float
    confidence_level: float
    training_null_search_count: int
    holdout_null_search_count: int
    expected_training_tail_count: int
    expected_holdout_exceedance_count: int
    holdout_design_far: float
    positive_injection_plan_ref: ArtifactRef
    training_count_basis: str
    holdout_count_basis: str

    SCHEMA_ID = "org.leo-flow.starlink-calibration-cell-plan"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink calibration cell plan schema")
        require_token(self.cell_id, "cell_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("channel_number must be one of 1, 2, 3, 4")
        for name in (
            "target_whole_search_far",
            "confidence_level",
            "holdout_design_far",
        ):
            require_positive(getattr(self, name), name)
            if getattr(self, name) >= 1:
                raise ValueError(f"{name} must lie below one")
        if self.confidence_level <= 0.5:
            raise ValueError("confidence_level must exceed one half")
        if self.holdout_design_far >= self.target_whole_search_far:
            raise ValueError("holdout design FAR must be below the target FAR")
        for name in (
            "training_null_search_count",
            "holdout_null_search_count",
            "expected_training_tail_count",
            "expected_holdout_exceedance_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        require_token(self.training_count_basis, "training_count_basis")
        require_token(self.holdout_count_basis, "holdout_count_basis")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkCalibrationCorpusItemV0_1:
    """One immutable whole-search score with independently asserted truth."""

    item_id: str
    cell_id: str
    role: StarlinkCalibrationCorpusRole
    recording_id: RecordingId
    recording_identity_digest: Digest
    candidate_id: str
    candidate_digest: Digest
    score: float
    truth_basis_ref: ArtifactRef
    injection_ref: ArtifactRef | None = None
    injection_snr_db: float | None = None

    def __post_init__(self) -> None:
        require_token(self.item_id, "item_id")
        require_token(self.cell_id, "cell_id")
        require_token(self.candidate_id, "candidate_id")
        require_finite(self.score, "score")
        if self.role is StarlinkCalibrationCorpusRole.POSITIVE_INJECTION:
            if self.injection_ref is None or self.injection_snr_db is None:
                raise ValueError(
                    "positive injection requires exact injection identity and SNR"
                )
            require_finite(self.injection_snr_db, "injection_snr_db")
        elif self.injection_ref is not None or self.injection_snr_db is not None:
            raise ValueError("null corpus item cannot carry an injection")


@dataclass(frozen=True)
class StarlinkCalibrationCorpusV0_1:
    """Locked train/holdout/injection corpus for one exact cell."""

    schema: SchemaRef
    corpus_id: str
    cell_plan_digest: Digest
    items: tuple[StarlinkCalibrationCorpusItemV0_1, ...]

    SCHEMA_ID = "org.leo-flow.starlink-calibration-corpus"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink calibration corpus schema")
        require_token(self.corpus_id, "corpus_id")
        if not self.items:
            raise ValueError("calibration corpus cannot be empty")
        item_ids = [item.item_id for item in self.items]
        candidate_ids = [item.candidate_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("calibration corpus item identities must be unique")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("a whole-search candidate cannot occur in two corpus rows")
        cell_ids = {item.cell_id for item in self.items}
        if len(cell_ids) != 1:
            raise ValueError("a calibration corpus cannot pool statistical cells")
        roles = {item.role for item in self.items}
        if (
            not {
                StarlinkCalibrationCorpusRole.TRAIN_NULL,
                StarlinkCalibrationCorpusRole.HOLDOUT_NULL,
                StarlinkCalibrationCorpusRole.POSITIVE_INJECTION,
            }
            <= roles
        ):
            raise ValueError(
                "calibration corpus requires train null, holdout null, and positive roles"
            )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkPositivePerformanceV0_1:
    injection_snr_db: float
    trial_count: int
    detection_count: int

    def __post_init__(self) -> None:
        require_finite(self.injection_snr_db, "injection_snr_db")
        if self.trial_count <= 0:
            raise ValueError("positive performance requires trials")
        if not 0 <= self.detection_count <= self.trial_count:
            raise ValueError("positive detection count is outside its trials")

    @property
    def detection_probability(self) -> float:
        return self.detection_count / self.trial_count


@dataclass(frozen=True)
class StarlinkCalibrationEvidenceV0_1:
    """Threshold fit on train nulls and audited exactly once on holdout nulls."""

    schema: SchemaRef
    evidence_id: str
    cell_plan_digest: Digest
    corpus_digest: Digest
    threshold: float
    training_null_search_count: int
    training_threshold_exceedance_count: int
    holdout_null_search_count: int
    holdout_threshold_exceedance_count: int
    holdout_far_upper_bound: float
    confidence_level: float
    target_whole_search_far: float
    interval_method: str
    accepted: bool
    positive_performance: tuple[StarlinkPositivePerformanceV0_1, ...]

    SCHEMA_ID = "org.leo-flow.starlink-calibration-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink calibration evidence schema")
        require_token(self.evidence_id, "evidence_id")
        require_finite(self.threshold, "threshold")
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
                raise ValueError(f"{label} exceedance count is outside its corpus")
        require_positive(self.holdout_far_upper_bound, "holdout_far_upper_bound")
        if self.holdout_far_upper_bound > 1:
            raise ValueError("holdout_far_upper_bound must not exceed one")
        for name in ("confidence_level", "target_whole_search_far"):
            require_positive(getattr(self, name), name)
            if getattr(self, name) >= 1:
                raise ValueError(f"{name} must lie below one")
        if self.interval_method != "one-sided-wilson-score":
            raise ValueError("unsupported FAR confidence interval method")
        expected_acceptance = (
            self.holdout_far_upper_bound <= self.target_whole_search_far
        )
        if self.accepted is not expected_acceptance:
            raise ValueError("calibration acceptance disagrees with held-out FAR bound")
        snrs = [item.injection_snr_db for item in self.positive_performance]
        if not snrs:
            raise ValueError(
                "calibration evidence requires positive-injection performance"
            )
        if len(snrs) != len(set(snrs)):
            raise ValueError("positive performance SNR points must be unique")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
