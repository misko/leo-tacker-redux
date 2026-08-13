"""Immutable, leakage-resistant dataset snapshots.

This module consumes identities and metadata produced by independent-recording
analysis.  It never opens IQ and never runs or imports a detector/model.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.core import Digest, canonical_digest


class DatasetPromotionError(ValueError):
    """A candidate snapshot cannot support the requested scientific claim."""


class DatasetSplit(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    LOCKED_TEST = "locked_test"


class LabelSource(str, Enum):
    OBSERVED = "observed"
    MANUAL = "manual"
    INJECTED = "injected"
    EPHEMERIS_DERIVED = "ephemeris_derived"
    PSEUDO_LABEL = "pseudo_label"
    UNLABELED = "unlabeled"


@dataclass(frozen=True)
class LabelEvidence:
    source: LabelSource
    evidence_digest: Digest
    producer_id: str
    produced_utc_ns: int
    independent_of_method_ids: tuple[str, ...]
    uncertainty: tuple[tuple[str, str], ...] = ()
    base_recording_digest: Digest | None = None
    injection_spec_digest: Digest | None = None

    def __post_init__(self) -> None:
        if not self.producer_id or any(
            character.isspace() for character in self.producer_id
        ):
            raise ValueError("producer_id must be a token")
        if self.produced_utc_ns < 0:
            raise ValueError("produced_utc_ns must be non-negative")
        if len(set(self.independent_of_method_ids)) != len(
            self.independent_of_method_ids
        ):
            raise ValueError("independent method IDs must be unique")
        has_injection_pair = (
            self.base_recording_digest is not None
            and self.injection_spec_digest is not None
        )
        if self.source is LabelSource.INJECTED and not has_injection_pair:
            raise ValueError(
                "injected truth must pin base recording and injection spec"
            )
        if self.source is not LabelSource.INJECTED and (
            self.base_recording_digest is not None
            or self.injection_spec_digest is not None
        ):
            raise ValueError("only injected truth may carry injection lineage")


@dataclass(frozen=True)
class TruthLabel:
    target_present: bool | None
    source: LabelSource
    evidence: tuple[LabelEvidence, ...]
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("label must carry evidence")
        if any(item.source is not self.source for item in self.evidence):
            raise ValueError("label and evidence sources differ")
        if self.source is LabelSource.UNLABELED and self.target_present is not None:
            raise ValueError("unlabeled data cannot be positive or negative")
        if self.source is not LabelSource.UNLABELED and self.target_present is None:
            raise ValueError("labeled data must state target presence")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must lie in [0, 1]")

    def usable_as_truth_for(self, evaluated_method_id: str) -> bool:
        if self.source in {
            LabelSource.UNLABELED,
            LabelSource.PSEUDO_LABEL,
            LabelSource.EPHEMERIS_DERIVED,
        }:
            return False
        return all(
            evaluated_method_id in evidence.independent_of_method_ids
            for evidence in self.evidence
        )


@dataclass(frozen=True)
class DatasetCandidate:
    feature_set_id: str
    feature_set_digest: Digest
    recording_id: str
    split_group_id: str
    captured_utc_ns: int
    radio_id: str
    lnb_ids: tuple[str, ...]
    observation_mode: str
    sample_rate_hz: int
    gain_mode: str
    gain_db: str | None
    satellite_id: str | None
    truth: TruthLabel
    scored_truth: bool = True
    derived_from_recording_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("feature_set_id", "recording_id", "split_group_id", "radio_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if self.captured_utc_ns < 0 or self.sample_rate_hz <= 0:
            raise ValueError("capture time and sample rate must be valid")
        if not self.lnb_ids or len(set(self.lnb_ids)) != len(self.lnb_ids):
            raise ValueError("lnb_ids must be non-empty and unique")


@dataclass(frozen=True)
class SplitDiagnostics:
    candidate_count: int
    group_count: int
    by_split: tuple[tuple[str, int], ...]
    strata: tuple[tuple[str, tuple[tuple[str, tuple[tuple[str, int], ...]], ...]], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DatasetSnapshot:
    evaluated_method_id: str
    ordered_members: tuple[tuple[str, str, str, bool], ...]
    membership_digest: Digest
    diagnostics: SplitDiagnostics
    promoted: bool

    def __post_init__(self) -> None:
        if self.membership_digest != canonical_digest(self.ordered_members):
            raise ValueError("membership digest does not match ordered members")


def carve_dataset(
    candidates: Iterable[DatasetCandidate],
    *,
    group_partitions: Mapping[str, DatasetSplit],
    evaluated_method_id: str,
    require_promotion: bool = False,
) -> DatasetSnapshot:
    """Freeze explicit group-level membership; no random split is performed."""

    materialized = tuple(candidates)
    if not materialized:
        raise ValueError("dataset cannot be empty")
    feature_ids = [item.feature_set_id for item in materialized]
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError("feature_set_id membership must be unique")
    missing_groups = sorted(
        {item.split_group_id for item in materialized} - set(group_partitions)
    )
    if missing_groups:
        raise ValueError(f"groups lack explicit partition assignment: {missing_groups}")

    # An injection and its real-noise parent must remain in the same group.
    group_by_recording = {
        item.recording_id: item.split_group_id for item in materialized
    }
    for item in materialized:
        parent = item.derived_from_recording_id
        if (
            parent in group_by_recording
            and group_by_recording[parent] != item.split_group_id
        ):
            raise ValueError(
                f"injection/base leakage: {item.recording_id} and {parent} differ"
            )

    split_order = {
        DatasetSplit.TRAIN: 0,
        DatasetSplit.VALIDATION: 1,
        DatasetSplit.LOCKED_TEST: 2,
    }
    ordered = tuple(
        sorted(
            materialized,
            key=lambda item: (
                split_order[group_partitions[item.split_group_id]],
                item.captured_utc_ns,
                item.feature_set_id,
            ),
        )
    )
    membership = tuple(
        (
            item.feature_set_id,
            str(item.feature_set_digest),
            group_partitions[item.split_group_id].value,
            item.scored_truth,
        )
        for item in ordered
    )
    diagnostics = _diagnostics(ordered, group_partitions, evaluated_method_id)
    promoted = not diagnostics.warnings
    if require_promotion and not promoted:
        raise DatasetPromotionError("; ".join(diagnostics.warnings))
    return DatasetSnapshot(
        evaluated_method_id=evaluated_method_id,
        ordered_members=membership,
        membership_digest=canonical_digest(membership),
        diagnostics=diagnostics,
        promoted=promoted,
    )


def _diagnostics(
    candidates: tuple[DatasetCandidate, ...],
    assignments: Mapping[str, DatasetSplit],
    evaluated_method_id: str,
) -> SplitDiagnostics:
    by_split = Counter(assignments[item.split_group_id].value for item in candidates)
    dimensions: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for item in candidates:
        split = assignments[item.split_group_id].value
        values = {
            "radio": (item.radio_id,),
            "lnb": item.lnb_ids,
            "mode": (item.observation_mode,),
            "rate_hz": (str(item.sample_rate_hz),),
            "gain": (f"{item.gain_mode}:{item.gain_db or 'unspecified'}",),
            "satellite": (item.satellite_id or "unknown",),
            "utc_day": (str(item.captured_utc_ns // 86_400_000_000_000),),
            "truth_source": (item.truth.source.value,),
            "truth_role": ("scored" if item.scored_truth else "context_only",),
        }
        for dimension, dimension_values in values.items():
            for value in dimension_values:
                dimensions[dimension][value][split] += 1
    strata = tuple(
        (
            dimension,
            tuple(
                (value, tuple(sorted(counts.items())))
                for value, counts in sorted(values.items())
            ),
        )
        for dimension, values in sorted(dimensions.items())
    )
    warnings: list[str] = []
    if len({item.split_group_id for item in candidates}) < 3:
        warnings.append("fewer-than-three-independent-split-groups")
    absent = [split.value for split in DatasetSplit if by_split[split.value] == 0]
    if absent:
        warnings.append("empty-partitions:" + ",".join(absent))
    unusable = [
        item.feature_set_id
        for item in candidates
        if item.scored_truth and not item.truth.usable_as_truth_for(evaluated_method_id)
    ]
    if unusable:
        warnings.append(f"non-independent-or-non-truth-labels:{len(unusable)}")
    if not any(
        item.scored_truth and item.truth.target_present is False for item in candidates
    ):
        warnings.append("no-independent-negatives")
    if not any(
        item.scored_truth and item.truth.target_present is True for item in candidates
    ):
        warnings.append("no-independent-positives")
    if not any(
        item.scored_truth and item.truth.source is LabelSource.INJECTED
        for item in candidates
    ):
        warnings.append("no-exact-injection-truth")
    time_ranges = {
        split: [
            item.captured_utc_ns
            for item in candidates
            if assignments[item.split_group_id] is split
        ]
        for split in DatasetSplit
    }
    if all(time_ranges.values()) and not (
        max(time_ranges[DatasetSplit.TRAIN])
        <= min(time_ranges[DatasetSplit.VALIDATION])
        and max(time_ranges[DatasetSplit.VALIDATION])
        <= min(time_ranges[DatasetSplit.LOCKED_TEST])
    ):
        warnings.append("partitions-are-not-time-ordered")
    return SplitDiagnostics(
        candidate_count=len(candidates),
        group_count=len({item.split_group_id for item in candidates}),
        by_split=tuple(sorted(by_split.items())),
        strata=strata,
        warnings=tuple(warnings),
    )
