from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import DatasetSplit
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
from leo_flow.contracts.features import FeatureSetBundle, MethodScore
from tests.integration.starlink_e2e_calibration import (
    FrozenTrainCalibrationMember,
    calibrate_train_thresholds,
)

METHODS = ("energy@1", "paired@1")


def _score(method: str, window: int, value: float) -> MethodScore:
    name, version = method.split("@")
    return MethodScore(
        name,
        version,
        SegmentId("seg_calibration"),
        "rxpair_calibration",
        window * 100,
        (window + 1) * 100,
        value,
        "fixture-score",
    )


def _bundle(index: int, scores: dict[str, tuple[float, ...]]) -> FeatureSetBundle:
    rows = tuple(
        _score(method, window, value)
        for method in sorted(scores)
        for window, value in enumerate(scores[method])
    )
    return FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        FeatureSetId(f"fset_train_{index}"),
        AnalysisRunId(f"arun_train_{index}"),
        RecordingId(f"rec_train_{index}"),
        Digest.sha256(f"recording-{index}".encode()),
        Provenance(
            "calibration-fixture",
            "1",
            "commit",
            Digest.sha256(b"environment"),
            Digest.sha256(b"config"),
            (Digest.sha256(f"recording-{index}".encode()),),
            (Digest.sha256(b"algorithm"),),
            UtcNs(index),
            UtcNs(index + 1),
            "test-host",
        ),
        (),
        rows,
    )


def _member(
    index: int, label: bool, energy: tuple[float, ...], paired: tuple[float, ...]
) -> FrozenTrainCalibrationMember:
    return FrozenTrainCalibrationMember(
        _bundle(index, {"energy@1": energy, "paired@1": paired}),
        label,
        f"group_{index}",
    )


def _fixture() -> tuple[FrozenTrainCalibrationMember, ...]:
    return (
        _member(1, True, (0.2, 0.9), (0.4,)),
        _member(2, True, (0.8,), (0.3,)),
        _member(3, False, (0.7,), (0.2,)),
        _member(4, False, (0.1,), (0.5,)),
    )


def test_calibration_uses_recording_maxima_and_binds_canonical_train_input() -> None:
    members = _fixture()
    first = calibrate_train_thresholds(members, expected_method_ids=METHODS)
    reordered = calibrate_train_thresholds(
        tuple(reversed(members)), expected_method_ids=tuple(reversed(METHODS))
    )

    assert first == reordered
    assert first.thresholds == (("energy@1", 0.8), ("paired@1", 0.3))
    assert first.calibration_dataset_id.startswith("dataset_traincal_")
    assert first.rule_id.startswith("rule_traincal_")

    relabeled = list(members)
    relabeled[0] = replace(relabeled[0], target_present=False)
    changed = calibrate_train_thresholds(tuple(relabeled), expected_method_ids=METHODS)
    assert changed.calibration_dataset_id != first.calibration_dataset_id
    assert changed.rule_id != first.rule_id


def test_equal_balanced_accuracy_chooses_higher_threshold() -> None:
    members = (
        _member(1, True, (0.8,), (0.8,)),
        _member(2, True, (0.4,), (0.4,)),
        _member(3, False, (0.6,), (0.6,)),
        _member(4, False, (0.2,), (0.2,)),
    )
    rule = calibrate_train_thresholds(members, expected_method_ids=METHODS)
    assert rule.thresholds == (("energy@1", 0.8), ("paired@1", 0.8))


def test_each_recording_contributes_one_maximum_not_one_vote_per_window() -> None:
    many_lower_windows = (0.65,) * 100
    members = (
        _member(1, True, (0.9,), (0.9,)),
        _member(2, True, many_lower_windows, many_lower_windows),
        _member(3, False, (0.7,), (0.7,)),
        _member(4, False, (0.5,), (0.5,)),
    )

    rule = calibrate_train_thresholds(members, expected_method_ids=METHODS)

    # Recording-level 0.65 and 0.9 both have balanced accuracy 0.75, so the
    # conservative tie-break selects 0.9. Window-weighted fitting would select
    # 0.65 because the second positive recording has 100 windows.
    assert rule.thresholds == (("energy@1", 0.9), ("paired@1", 0.9))


@pytest.mark.parametrize("label", (True, False))
def test_calibration_rejects_a_missing_binary_class(label: bool) -> None:
    members = (
        _member(1, label, (0.9,), (0.9,)),
        _member(2, label, (0.1,), (0.1,)),
    )
    with pytest.raises(ValueError, match="both binary classes"):
        calibrate_train_thresholds(members, expected_method_ids=METHODS)


def test_calibration_rejects_missing_or_unexpected_exact_methods() -> None:
    complete = _fixture()[0]
    missing = FrozenTrainCalibrationMember(
        _bundle(5, {"energy@1": (0.1,)}), False, "group_5"
    )
    with pytest.raises(ValueError, match="missing=.*paired@1"):
        calibrate_train_thresholds((complete, missing), expected_method_ids=METHODS)

    with pytest.raises(ValueError, match="unexpected=.*paired@1"):
        calibrate_train_thresholds(
            (_member(6, True, (0.8,), (0.8,)), _member(7, False, (0.2,), (0.2,))),
            expected_method_ids=("energy@1",),
        )


def test_non_train_membership_is_unrepresentable_at_calibration_boundary() -> None:
    with pytest.raises(ValueError, match="TRAIN members only"):
        FrozenTrainCalibrationMember(
            _bundle(1, {"energy@1": (0.5,), "paired@1": (0.5,)}),
            True,
            "group_validation",
            split=DatasetSplit.VALIDATION,
        )


def test_duplicate_recording_membership_and_invalid_method_identity_fail() -> None:
    first = _fixture()[0]
    duplicate = replace(
        _fixture()[1],
        bundle=replace(_fixture()[1].bundle, recording_id=first.bundle.recording_id),
    )
    with pytest.raises(ValueError, match="exactly one FeatureSet"):
        calibrate_train_thresholds((first, duplicate), expected_method_ids=METHODS)
    with pytest.raises(ValueError, match="method@version"):
        calibrate_train_thresholds(_fixture(), expected_method_ids=("energy",))
