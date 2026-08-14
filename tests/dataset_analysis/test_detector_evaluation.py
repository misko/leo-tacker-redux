from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetCandidate,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    carve_dataset,
    evaluate_detectors,
    freeze_dataset_snapshot,
)
from leo_flow.analysis.recording import ThresholdRule, encode_feature_set
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    FeatureSetId,
    Provenance,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef, MethodScore
from leo_flow.contracts.storage import ObjectRef


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _truth(source: LabelSource, present: bool | None) -> TruthLabel:
    injection = (
        {
            "base_recording_digest": _digest("base-noise"),
            "injection_spec_digest": _digest("injection-spec"),
        }
        if source is LabelSource.INJECTED
        else {}
    )
    return TruthLabel(
        present,
        source,
        (
            LabelEvidence(
                source,
                _digest(f"truth-{source.value}-{present}"),
                "independent-fixture",
                1,
                ("a@1", "b@1"),
                **injection,
            ),
        ),
        None if present is None else 1.0,
    )


def _score(method: str, window: int, value: float) -> MethodScore:
    return MethodScore(
        method,
        "1",
        SegmentId("seg_shared"),
        "rxpair_a_b",
        window * 100,
        (window + 1) * 100,
        value,
        "fixture-score",
    )


def _bundle(index: int, scores: tuple[MethodScore, ...]) -> FeatureSetBundle:
    return FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        FeatureSetId(f"fset_{index}"),
        AnalysisRunId(f"arun_{index}"),
        RecordingId(f"rec_{index}"),
        _digest(f"recording-{index}"),
        Provenance(
            "fixture-producer",
            "1",
            "commit",
            _digest("environment"),
            _digest("config"),
            (_digest(f"recording-{index}"),),
            (_digest("algorithm"),),
            UtcNs(index),
            UtcNs(index + 1),
            "test-host",
        ),
        (),
        scores,
    )


def _fixture():
    bundles = {
        "fset_1": _bundle(
            1,
            (
                _score("a", 0, 1),
                _score("b", 0, 1),
                _score("a", 1, 1),
                _score("b", 1, 0),
            ),
        ),
        # The repeated segment/window IDs prove FeatureSets are separate namespaces.
        "fset_2": _bundle(2, (_score("a", 0, 0),)),
        "fset_3": _bundle(3, (_score("a", 0, 1), _score("b", 0, 0))),
        "fset_4": _bundle(4, (_score("a", 0, 0), _score("b", 0, 1))),
    }
    truth = {
        1: _truth(LabelSource.INJECTED, True),
        2: _truth(LabelSource.MANUAL, False),
        3: _truth(LabelSource.OBSERVED, True),
        4: _truth(LabelSource.PSEUDO_LABEL, True),
    }
    candidates = []
    refs = []
    for index in range(1, 5):
        payload = encode_feature_set(bundles[f"fset_{index}"])
        digest = Digest.sha256(payload)
        candidates.append(
            DatasetCandidate(
                f"fset_{index}",
                digest,
                f"rec_{index}",
                f"group_{index}",
                index * 1_000,
                "radio_v5",
                ("lnb_a",),
                "fixture",
                1_000,
                "manual",
                "10",
                None,
                truth[index],
            )
        )
        refs.append(
            FeatureSetRef(
                FeatureSetId(f"fset_{index}"),
                AnalysisRunId(f"arun_{index}"),
                ObjectRef(
                    digest,
                    len(payload),
                    "application/json",
                    "feature-set-bundle-v0.1",
                    f"memory://fset-{index}",
                ),
            )
        )
    assignments = {
        "group_1": DatasetSplit.TRAIN,
        "group_2": DatasetSplit.VALIDATION,
        "group_3": DatasetSplit.LOCKED_TEST,
        "group_4": DatasetSplit.LOCKED_TEST,
    }
    carved = carve_dataset(
        candidates,
        group_partitions=assignments,
        evaluated_method_id="a@1",
    )
    dataset = freeze_dataset_snapshot(
        carved,
        candidates,
        refs,
        selection_spec="deterministic-evaluation-fixture-v1",
        selection_cutoff_utc_ns=UtcNs(10_000),
    )
    rule = ThresholdRule(
        "rule_frozen", "dataset_train_only", (("b@1", 0.5), ("a@1", 0.5))
    )
    return dataset, bundles, rule


def test_report_is_deterministic_namespaced_and_pairwise_complete() -> None:
    dataset, bundles, rule = _fixture()
    first = evaluate_detectors(dataset, bundles, rule)
    second = evaluate_detectors(dataset, dict(reversed(tuple(bundles.items()))), rule)

    assert first == second
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == Digest.sha256(first.canonical_bytes())
    assert first.overall_association.method_ids == ("a@1", "b@1")
    assert first.overall_association.union_window_count == 5
    assert first.overall_association.shared_window_count == ((5, 4), (4, 4))
    assert first.overall_association.shared_sample_count == ((500, 400), (400, 400))
    assert first.overall_association.missing_window_count == (0, 1)
    assert first.overall_association.firing_covariance[0][1] == pytest.approx(-0.125)
    assert first.overall_association.phi[0][1] == pytest.approx(-((1 / 3) ** 0.5))


def test_coverage_and_truth_never_turn_missing_or_proxy_into_negative() -> None:
    dataset, bundles, rule = _fixture()
    report = evaluate_detectors(dataset, bundles, rule)
    methods = {item.method_id: item for item in report.methods}
    a_splits = {item.split: item for item in methods["a@1"].by_split}
    b_splits = {item.split: item for item in methods["b@1"].by_split}

    assert a_splits["train"].firing_count == 2
    assert a_splits["train"].truth.true_positive == 1
    assert a_splits["validation"].truth.true_negative == 1
    assert a_splits["locked_test"].truth.true_positive == 1
    assert a_splits["locked_test"].truth.inadmissible_truth_count == 1

    validation_b = b_splits["validation"]
    assert validation_b.feature_set_present_count == 0
    assert validation_b.missing_window_count == 1
    assert validation_b.truth.admissible_truth_count == 1
    assert validation_b.truth.scored_prediction_count == 0
    assert validation_b.truth.missing_prediction_count == 1
    assert validation_b.truth.true_negative == 0
    assert b_splits["locked_test"].truth.false_negative == 1
    assert b_splits["locked_test"].truth.inadmissible_truth_count == 1


def test_exact_feature_membership_and_train_calibration_are_enforced() -> None:
    dataset, bundles, rule = _fixture()
    with pytest.raises(ValueError, match="membership differs"):
        evaluate_detectors(dataset, {"fset_1": bundles["fset_1"]}, rule)
    changed = dict(bundles)
    changed["fset_1"] = replace(bundles["fset_1"], warnings=("changed",))
    with pytest.raises(ValueError, match="bytes do not match"):
        evaluate_detectors(dataset, changed, rule)
    with pytest.raises(ValueError, match="restricted to the train"):
        evaluate_detectors(
            dataset,
            bundles,
            rule,
            threshold_calibration_split=DatasetSplit.VALIDATION,
        )


def test_scores_without_a_frozen_threshold_are_rejected() -> None:
    dataset, bundles, _ = _fixture()
    with pytest.raises(ValueError, match="no entry for b@1"):
        evaluate_detectors(
            dataset,
            bundles,
            ThresholdRule("rule_incomplete", "dataset_train_only", (("a@1", 0.5),)),
        )
