"""Deterministic detector reports over frozen FeatureSet dataset membership.

This module applies an already-calibrated threshold rule.  It neither opens IQ
nor fits thresholds, labels, or detectors.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from leo_flow.analysis.recording.codec import encode_feature_set
from leo_flow.analysis.recording.decisions import ThresholdRule
from leo_flow.contracts.core import (
    Digest,
    SchemaRef,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.features import FeatureSetBundle, MethodScore

from .api import DatasetSplit
from .association import MethodAssociationReport
from .snapshot import DatasetMember, DatasetRole, DatasetSnapshotBundle

DETECTOR_EVALUATION_MEDIA_TYPE = "application/json"
DETECTOR_EVALUATION_FORMAT_ID = "detector-evaluation-report-v0.1"


@dataclass(frozen=True)
class BinaryClassificationCounts:
    """Recording-level counts; a prediction is true when any window fires."""

    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    admissible_truth_count: int
    scored_prediction_count: int
    missing_prediction_count: int
    inadmissible_truth_count: int
    context_only_count: int


@dataclass(frozen=True)
class SplitMethodReport:
    split: str
    feature_set_count: int
    feature_set_present_count: int
    union_window_count: int
    present_window_count: int
    missing_window_count: int
    firing_count: int
    truth: BinaryClassificationCounts


@dataclass(frozen=True)
class MethodEvaluation:
    method_id: str
    threshold: float
    score_semantics: str | None
    by_split: tuple[SplitMethodReport, ...]


@dataclass(frozen=True)
class SplitAssociationReport:
    split: str
    association: MethodAssociationReport


@dataclass(frozen=True)
class DetectorEvaluationReport:
    """Canonical report whose digest is its content address."""

    schema: SchemaRef
    dataset_snapshot_id: str
    dataset_snapshot_digest: Digest
    feature_membership_digest: Digest
    threshold_rule_id: str
    threshold_rule_digest: Digest
    threshold_calibration_dataset_id: str
    threshold_calibration_split: str
    methods: tuple[MethodEvaluation, ...]
    overall_association: MethodAssociationReport
    association_by_split: tuple[SplitAssociationReport, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.detector-evaluation-report"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported detector evaluation schema")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


@dataclass(frozen=True)
class _Window:
    feature_set_id: str
    split: DatasetSplit
    segment_id: str
    receiver_key: str
    start: int
    stop: int


def evaluate_detectors(
    dataset: DatasetSnapshotBundle,
    feature_sets: Mapping[str, FeatureSetBundle],
    threshold_rule: ThresholdRule,
    *,
    threshold_calibration_split: DatasetSplit = DatasetSplit.TRAIN,
) -> DetectorEvaluationReport:
    """Apply one frozen rule and report coverage, association, and safe truth.

    Accuracy is recording-level: a method predicts present when any emitted
    window fires.  A FeatureSet with no score for a method is missing, not a
    negative prediction.  Truth enters a denominator only when its dataset role
    is scored and every evidence item declares independence from that method;
    pseudo, ephemeris-derived, and unlabeled evidence is always excluded by the
    existing ``TruthLabel`` contract.
    """

    if threshold_calibration_split is not DatasetSplit.TRAIN:
        raise ValueError("threshold calibration is restricted to the train split")
    thresholds = dict(threshold_rule.thresholds)
    method_ids = tuple(sorted(thresholds))
    if len(thresholds) != len(threshold_rule.thresholds):
        raise ValueError("threshold method identities must be unique")

    member_by_feature = {
        str(member.feature_set_ref.feature_set_id): member for member in dataset.members
    }
    if set(feature_sets) != set(member_by_feature):
        missing = sorted(set(member_by_feature) - set(feature_sets))
        extra = sorted(set(feature_sets) - set(member_by_feature))
        raise ValueError(
            f"FeatureSet membership differs: missing={missing}, extra={extra}"
        )

    rows: dict[_Window, dict[str, bool]] = defaultdict(dict)
    present_by_feature: dict[str, set[str]] = defaultdict(set)
    fired_by_feature: dict[str, set[str]] = defaultdict(set)
    semantics: dict[str, set[str]] = defaultdict(set)
    for feature_id in sorted(feature_sets):
        bundle = feature_sets[feature_id]
        member = member_by_feature[feature_id]
        _verify_feature_set(member, bundle)
        for score in bundle.method_scores:
            method = _method_identity(score)
            if method not in thresholds:
                raise ValueError(f"threshold rule has no entry for {method}")
            key = _Window(
                feature_id,
                member.split,
                str(score.segment_id),
                score.receiver_key,
                score.window_start_sample,
                score.window_stop_sample,
            )
            if method in rows[key]:
                raise ValueError(f"duplicate method score for shared window: {method}")
            fired = score.score >= thresholds[method]
            rows[key][method] = fired
            present_by_feature[feature_id].add(method)
            semantics[method].add(score.score_semantics)
            if fired:
                fired_by_feature[feature_id].add(method)

    inconsistent_semantics = sorted(
        method for method, values in semantics.items() if len(values) > 1
    )
    if inconsistent_semantics:
        raise ValueError(
            f"one method identity has multiple score semantics: {inconsistent_semantics}"
        )

    split_reports: dict[DatasetSplit, MethodAssociationReport] = {}
    for split in DatasetSplit:
        split_reports[split] = _association(
            {key: row for key, row in rows.items() if key.split is split}, method_ids
        )
    overall = _association(rows, method_ids)
    methods = tuple(
        MethodEvaluation(
            method_id=method,
            threshold=thresholds[method],
            score_semantics=next(iter(semantics[method]), None),
            by_split=tuple(
                _split_method_report(
                    split,
                    method,
                    dataset.members,
                    rows,
                    present_by_feature,
                    fired_by_feature,
                )
                for split in DatasetSplit
            ),
        )
        for method in method_ids
    )
    warnings = list(dataset.promotion_warnings)
    warnings.append("calibration-dataset-split-membership-is-operator-attested")
    if all(not bundle.method_scores for bundle in feature_sets.values()):
        warnings.append("no-method-scores")
    return DetectorEvaluationReport(
        schema=SchemaRef(DetectorEvaluationReport.SCHEMA_ID),
        dataset_snapshot_id=str(dataset.feature_dataset.snapshot_id),
        dataset_snapshot_digest=dataset.snapshot_digest,
        feature_membership_digest=dataset.feature_dataset.membership_digest,
        threshold_rule_id=threshold_rule.rule_id,
        threshold_rule_digest=threshold_rule.digest,
        threshold_calibration_dataset_id=threshold_rule.calibration_dataset_id,
        threshold_calibration_split=threshold_calibration_split.value,
        methods=methods,
        overall_association=overall,
        association_by_split=tuple(
            SplitAssociationReport(split.value, split_reports[split])
            for split in DatasetSplit
        ),
        warnings=tuple(warnings),
    )


def _verify_feature_set(member: DatasetMember, bundle: FeatureSetBundle) -> None:
    ref = member.feature_set_ref
    if bundle.feature_set_id != ref.feature_set_id:
        raise ValueError("FeatureSet ID does not match frozen dataset membership")
    if bundle.analysis_run_id != ref.analysis_run_id:
        raise ValueError("analysis run does not match frozen dataset membership")
    if Digest.sha256(encode_feature_set(bundle)) != ref.bundle_ref.digest:
        raise ValueError("FeatureSet bytes do not match frozen dataset digest")


def _method_identity(score: MethodScore) -> str:
    return f"{score.method_id}@{score.method_version}"


def _split_method_report(
    split: DatasetSplit,
    method: str,
    members: tuple[DatasetMember, ...],
    rows: Mapping[_Window, Mapping[str, bool]],
    present_by_feature: Mapping[str, set[str]],
    fired_by_feature: Mapping[str, set[str]],
) -> SplitMethodReport:
    split_members = tuple(member for member in members if member.split is split)
    split_rows = tuple((key, row) for key, row in rows.items() if key.split is split)
    present_windows = sum(method in row for _, row in split_rows)
    truth_counts: Counter[str] = Counter()
    for member in split_members:
        feature_id = str(member.feature_set_ref.feature_set_id)
        if member.role is DatasetRole.CONTEXT_ONLY:
            truth_counts["context"] += 1
            continue
        truth = member.truth
        if not truth.usable_as_truth_for(method):
            truth_counts["inadmissible"] += 1
            continue
        if method not in present_by_feature.get(feature_id, set()):
            truth_counts["missing"] += 1
            continue
        predicted = method in fired_by_feature.get(feature_id, set())
        if truth.target_present is True:
            truth_counts["tp" if predicted else "fn"] += 1
        elif truth.target_present is False:
            truth_counts["fp" if predicted else "tn"] += 1
        else:  # Defensive: usable_as_truth_for excludes unlabeled truth.
            raise ValueError("admissible truth has no target")
    scored = sum(truth_counts[key] for key in ("tp", "fp", "tn", "fn"))
    return SplitMethodReport(
        split=split.value,
        feature_set_count=len(split_members),
        feature_set_present_count=sum(
            method
            in present_by_feature.get(str(item.feature_set_ref.feature_set_id), set())
            for item in split_members
        ),
        union_window_count=len(split_rows),
        present_window_count=present_windows,
        missing_window_count=len(split_rows) - present_windows,
        firing_count=sum(row.get(method, False) for _, row in split_rows),
        truth=BinaryClassificationCounts(
            true_positive=truth_counts["tp"],
            false_positive=truth_counts["fp"],
            true_negative=truth_counts["tn"],
            false_negative=truth_counts["fn"],
            admissible_truth_count=scored + truth_counts["missing"],
            scored_prediction_count=scored,
            missing_prediction_count=truth_counts["missing"],
            inadmissible_truth_count=truth_counts["inadmissible"],
            context_only_count=truth_counts["context"],
        ),
    )


def _association(
    rows: Mapping[_Window, Mapping[str, bool]], method_ids: tuple[str, ...]
) -> MethodAssociationReport:
    covariance: list[list[float | None]] = []
    phi: list[list[float | None]] = []
    shared_windows: list[list[int]] = []
    shared_samples: list[list[int]] = []
    for left in method_ids:
        cov_row: list[float | None] = []
        phi_row: list[float | None] = []
        window_row: list[int] = []
        sample_row: list[int] = []
        for right in method_ids:
            shared = [
                (key, row[left], row[right])
                for key, row in rows.items()
                if left in row and right in row
            ]
            window_row.append(len(shared))
            sample_row.append(sum(key.stop - key.start for key, _, _ in shared))
            if not shared:
                cov_row.append(None)
                phi_row.append(None)
                continue
            xs = [float(left_value) for _, left_value, _ in shared]
            ys = [float(right_value) for _, _, right_value in shared]
            left_mean = math.fsum(xs) / len(xs)
            right_mean = math.fsum(ys) / len(ys)
            cov = math.fsum(
                (x - left_mean) * (y - right_mean) for x, y in zip(xs, ys, strict=True)
            ) / len(xs)
            left_var = math.fsum((x - left_mean) ** 2 for x in xs) / len(xs)
            right_var = math.fsum((y - right_mean) ** 2 for y in ys) / len(ys)
            cov_row.append(cov)
            phi_row.append(
                cov / math.sqrt(left_var * right_var)
                if left_var and right_var
                else None
            )
        covariance.append(cov_row)
        phi.append(phi_row)
        shared_windows.append(window_row)
        shared_samples.append(sample_row)
    present = tuple(
        sum(method in row for row in rows.values()) for method in method_ids
    )
    return MethodAssociationReport(
        method_ids=method_ids,
        firing_covariance=tuple(tuple(row) for row in covariance),
        phi=tuple(tuple(row) for row in phi),
        shared_window_count=tuple(tuple(row) for row in shared_windows),
        shared_sample_count=tuple(tuple(row) for row in shared_samples),
        method_present_window_count=present,
        union_window_count=len(rows),
        missing_window_count=tuple(len(rows) - count for count in present),
    )
