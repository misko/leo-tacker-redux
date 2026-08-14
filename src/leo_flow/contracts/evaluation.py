"""Additive references and read-only views for detector evaluations."""

from __future__ import annotations

from dataclasses import dataclass

from .core import DetectorEvaluationId, Digest, EvaluationRunId
from .storage import ObjectRef


@dataclass(frozen=True)
class DetectorEvaluationRef:
    evaluation_id: DetectorEvaluationId
    run_id: EvaluationRunId
    report_digest: Digest
    report_object: ObjectRef

    def __post_init__(self) -> None:
        if self.report_digest != self.report_object.digest:
            raise ValueError("report digest and object digest must match")


@dataclass(frozen=True)
class DetectorMethodSplitSummary:
    method_id: str
    split: str
    threshold: float
    score_semantics: str | None
    feature_set_count: int
    feature_set_present_count: int
    union_window_count: int
    present_window_count: int
    missing_window_count: int
    firing_count: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    scored_prediction_count: int
    missing_prediction_count: int


@dataclass(frozen=True)
class DetectorEvaluationView:
    ref: DetectorEvaluationRef
    dataset_snapshot_id: str
    dataset_snapshot_digest: Digest
    feature_membership_digest: Digest
    threshold_rule_id: str
    threshold_rule_digest: Digest
    calibration_dataset_id: str
    calibration_split: str
    method_count: int
    union_window_count: int
    warnings: tuple[str, ...]
    methods: tuple[DetectorMethodSplitSummary, ...]
