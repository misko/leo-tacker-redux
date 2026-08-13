from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetCandidate,
    DatasetPromotionError,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    carve_dataset,
)
from leo_flow.contracts.core import Digest


def digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def label(
    source: LabelSource,
    present: bool | None,
    *,
    independent: tuple[str, ...] = ("method-a",),
) -> TruthLabel:
    kwargs = {}
    if source is LabelSource.INJECTED:
        kwargs = {
            "base_recording_digest": digest("base"),
            "injection_spec_digest": digest("spec"),
        }
    return TruthLabel(
        target_present=present,
        source=source,
        evidence=(
            LabelEvidence(
                source=source,
                evidence_digest=digest(f"evidence-{source.value}-{present}"),
                producer_id="truth-fixture-v1",
                produced_utc_ns=10,
                independent_of_method_ids=independent,
                uncertainty=(("snr_db", "+/-0.5"),),
                **kwargs,
            ),
        ),
        confidence=None if present is None else 1.0,
    )


def candidate(
    index: int,
    group: str,
    truth: TruthLabel,
    *,
    radio: str = "radio-v5-a",
) -> DatasetCandidate:
    return DatasetCandidate(
        feature_set_id=f"fset-{index}",
        feature_set_digest=digest(f"feature-{index}"),
        recording_id=f"recording-{index}",
        split_group_id=group,
        captured_utc_ns=index * 86_400_000_000_000,
        radio_id=radio,
        lnb_ids=("lnb-a", "lnb-b"),
        observation_mode="narrow" if index % 2 else "wide",
        sample_rate_hz=2_000_000 if index % 2 else 30_720_000,
        gain_mode="manual",
        gain_db="50",
        satellite_id="25544" if index % 2 else None,
        truth=truth,
    )


def test_carving_is_explicit_deterministic_and_stratified() -> None:
    items = (
        candidate(3, "pass-c", label(LabelSource.MANUAL, False)),
        candidate(1, "pass-a", label(LabelSource.INJECTED, True)),
        candidate(2, "pass-b", label(LabelSource.OBSERVED, False)),
    )
    assignments = {
        "pass-a": DatasetSplit.TRAIN,
        "pass-b": DatasetSplit.VALIDATION,
        "pass-c": DatasetSplit.LOCKED_TEST,
    }
    first = carve_dataset(
        items,
        group_partitions=assignments,
        evaluated_method_id="method-a",
        require_promotion=True,
    )
    second = carve_dataset(
        reversed(items),
        group_partitions=assignments,
        evaluated_method_id="method-a",
        require_promotion=True,
    )

    assert first == second
    assert first.promoted is True
    assert [member[0] for member in first.ordered_members] == [
        "fset-1",
        "fset-2",
        "fset-3",
    ]
    assert first.diagnostics.by_split == (
        ("locked_test", 1),
        ("train", 1),
        ("validation", 1),
    )
    strata = dict(first.diagnostics.strata)
    assert "radio-v5-a" in dict(strata["radio"])
    assert set(strata) == {
        "gain",
        "lnb",
        "mode",
        "radio",
        "rate_hz",
        "satellite",
        "truth_role",
        "truth_source",
        "utc_day",
    }


@pytest.mark.parametrize(
    "source",
    [
        LabelSource.PSEUDO_LABEL,
        LabelSource.EPHEMERIS_DERIVED,
        LabelSource.UNLABELED,
    ],
)
def test_pseudo_ephemeris_and_unlabeled_are_not_promotable_truth(
    source: LabelSource,
) -> None:
    present = None if source is LabelSource.UNLABELED else True
    items = (
        candidate(1, "a", label(LabelSource.INJECTED, True)),
        candidate(2, "b", label(LabelSource.OBSERVED, False)),
        candidate(3, "c", label(source, present)),
    )
    with pytest.raises(DatasetPromotionError, match="non-independent-or-non-truth"):
        carve_dataset(
            items,
            group_partitions={
                "a": DatasetSplit.TRAIN,
                "b": DatasetSplit.VALIDATION,
                "c": DatasetSplit.LOCKED_TEST,
            },
            evaluated_method_id="method-a",
            require_promotion=True,
        )


def test_circular_manual_label_is_refused() -> None:
    circular = label(LabelSource.MANUAL, False, independent=("other-method",))
    items = (
        candidate(1, "a", label(LabelSource.INJECTED, True)),
        candidate(2, "b", label(LabelSource.OBSERVED, False)),
        candidate(3, "c", circular),
    )
    snapshot = carve_dataset(
        items,
        group_partitions={
            "a": DatasetSplit.TRAIN,
            "b": DatasetSplit.VALIDATION,
            "c": DatasetSplit.LOCKED_TEST,
        },
        evaluated_method_id="method-a",
    )
    assert snapshot.promoted is False
    assert "non-independent-or-non-truth-labels:1" in snapshot.diagnostics.warnings


def test_unlabeled_context_may_be_fitted_but_is_not_scored_as_truth() -> None:
    context = replace(
        candidate(4, "a", label(LabelSource.UNLABELED, None)),
        captured_utc_ns=86_400_000_000_000,
        scored_truth=False,
    )
    snapshot = carve_dataset(
        (
            candidate(1, "a", label(LabelSource.INJECTED, True)),
            candidate(2, "b", label(LabelSource.OBSERVED, False)),
            candidate(3, "c", label(LabelSource.MANUAL, False)),
            context,
        ),
        group_partitions={
            "a": DatasetSplit.TRAIN,
            "b": DatasetSplit.VALIDATION,
            "c": DatasetSplit.LOCKED_TEST,
        },
        evaluated_method_id="method-a",
        require_promotion=True,
    )
    assert snapshot.promoted is True
    assert snapshot.ordered_members[1][3] is False


def test_non_time_ordered_partitions_are_not_promotable() -> None:
    items = (
        candidate(3, "train-late", label(LabelSource.INJECTED, True)),
        candidate(2, "validation-middle", label(LabelSource.OBSERVED, False)),
        candidate(1, "test-early", label(LabelSource.MANUAL, False)),
    )
    snapshot = carve_dataset(
        items,
        group_partitions={
            "train-late": DatasetSplit.TRAIN,
            "validation-middle": DatasetSplit.VALIDATION,
            "test-early": DatasetSplit.LOCKED_TEST,
        },
        evaluated_method_id="method-a",
    )
    assert "partitions-are-not-time-ordered" in snapshot.diagnostics.warnings


def test_injection_and_base_cannot_cross_groups() -> None:
    base = candidate(1, "same-pass", label(LabelSource.OBSERVED, False))
    injection = replace(
        candidate(2, "other-pass", label(LabelSource.INJECTED, True)),
        derived_from_recording_id=base.recording_id,
    )
    with pytest.raises(ValueError, match="injection/base leakage"):
        carve_dataset(
            (base, injection),
            group_partitions={
                "same-pass": DatasetSplit.TRAIN,
                "other-pass": DatasetSplit.VALIDATION,
            },
            evaluated_method_id="method-a",
        )


def test_injected_evidence_requires_independent_lineage_hashes() -> None:
    with pytest.raises(ValueError, match="pin base recording and injection spec"):
        LabelEvidence(
            source=LabelSource.INJECTED,
            evidence_digest=digest("evidence"),
            producer_id="fixture",
            produced_utc_ns=1,
            independent_of_method_ids=("method-a",),
        )


def test_current_corpus_shape_is_deliberately_refused_for_promotion() -> None:
    # Wave 0 has only two conservative split groups, proxies/unlabeled sky, no
    # independent negatives, and no digital injection lineage.
    items = (
        candidate(1, "day-2026-08-13", label(LabelSource.PSEUDO_LABEL, True)),
        candidate(2, "day-2026-08-13", label(LabelSource.UNLABELED, None)),
        candidate(3, "day-2026-08-10", label(LabelSource.UNLABELED, None)),
    )
    snapshot = carve_dataset(
        items,
        group_partitions={
            "day-2026-08-13": DatasetSplit.TRAIN,
            "day-2026-08-10": DatasetSplit.VALIDATION,
        },
        evaluated_method_id="legacy-followup",
    )
    assert snapshot.promoted is False
    assert snapshot.diagnostics.warnings == (
        "fewer-than-three-independent-split-groups",
        "empty-partitions:locked_test",
        "non-independent-or-non-truth-labels:3",
        "no-independent-negatives",
        "no-exact-injection-truth",
    )
