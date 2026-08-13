"""Durable bridge from dataset carving to the frozen model dataset contract."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.core import (
    V0_1,
    DatasetSnapshotId,
    Digest,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    feature_dataset_membership_digest,
)

from .api import (
    DatasetCandidate,
    DatasetSnapshot,
    DatasetSplit,
    TruthLabel,
    carve_dataset,
)


class DatasetRole(str, Enum):
    """Whether a member enters accuracy denominators or provides context."""

    SCORED_TRUTH = "scored_truth"
    CONTEXT_ONLY = "context_only"


@dataclass(frozen=True)
class DatasetMember:
    """One fully attributed, immutable dataset member."""

    feature_set_ref: FeatureSetRef
    split_group_id: str
    split: DatasetSplit
    role: DatasetRole
    truth: TruthLabel

    def __post_init__(self) -> None:
        if not self.split_group_id or any(
            character.isspace() for character in self.split_group_id
        ):
            raise ValueError("split_group_id must be a token")


@dataclass(frozen=True)
class DatasetSnapshotRef:
    """Pins both model-compatible membership and rich scientific identity."""

    snapshot_id: DatasetSnapshotId
    feature_membership_digest: Digest
    snapshot_digest: Digest


@dataclass(frozen=True)
class DatasetSnapshotBundle:
    """Published snapshot with exact feature, partition, role, and truth identity."""

    schema: SchemaRef
    feature_dataset: FeatureDatasetSnapshot
    evaluated_method_id: str
    members: tuple[DatasetMember, ...]
    snapshot_digest: Digest
    promoted: bool
    promotion_warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dataset-snapshot-bundle"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported durable dataset snapshot schema")
        if not self.evaluated_method_id or any(
            character.isspace() for character in self.evaluated_method_id
        ):
            raise ValueError("evaluated_method_id must be a token")
        if not self.members:
            raise ValueError("dataset snapshot cannot be empty")
        member_refs = tuple(item.feature_set_ref for item in self.members)
        if member_refs != self.feature_dataset.ordered_feature_set_refs:
            raise ValueError("rich members do not match model feature membership")
        groups: dict[str, DatasetSplit] = {}
        for member in self.members:
            prior = groups.setdefault(member.split_group_id, member.split)
            if prior is not member.split:
                raise ValueError("one split group cannot cross partitions")
        if self.promoted != (not self.promotion_warnings):
            raise ValueError("promotion state and warnings disagree")
        if self.snapshot_digest != dataset_snapshot_digest(
            self.feature_dataset,
            self.evaluated_method_id,
            self.members,
            self.promoted,
            self.promotion_warnings,
        ):
            raise ValueError("snapshot digest does not match durable membership")

    @property
    def ref(self) -> DatasetSnapshotRef:
        return DatasetSnapshotRef(
            snapshot_id=self.feature_dataset.snapshot_id,
            feature_membership_digest=self.feature_dataset.membership_digest,
            snapshot_digest=self.snapshot_digest,
        )

    def members_in(
        self, split: DatasetSplit, *, role: DatasetRole | None = None
    ) -> tuple[DatasetMember, ...]:
        """Select from frozen assignments without deriving or shuffling a split."""

        return tuple(
            member
            for member in self.members
            if member.split is split and (role is None or member.role is role)
        )


def dataset_snapshot_digest(
    feature_dataset: FeatureDatasetSnapshot,
    evaluated_method_id: str,
    members: tuple[DatasetMember, ...],
    promoted: bool,
    promotion_warnings: tuple[str, ...],
) -> Digest:
    """Hash scientific identity while excluding replaceable object locators."""

    return canonical_digest(
        {
            "feature_membership_digest": str(feature_dataset.membership_digest),
            "selection_spec": feature_dataset.selection_spec,
            "selection_cutoff_utc_ns": feature_dataset.selection_cutoff_utc_ns,
            "evaluated_method_id": evaluated_method_id,
            "members": [_member_identity(member) for member in members],
            "promoted": promoted,
            "promotion_warnings": promotion_warnings,
        }
    )


def freeze_dataset_snapshot(
    carved: DatasetSnapshot,
    candidates: Iterable[DatasetCandidate],
    feature_set_refs: Iterable[FeatureSetRef],
    *,
    selection_spec: str,
    selection_cutoff_utc_ns: UtcNs,
) -> DatasetSnapshotBundle:
    """Reconcile a policy-rich carve with the unchanged model snapshot contract."""

    if not selection_spec:
        raise ValueError("selection_spec must be non-empty")
    materialized_candidates = tuple(candidates)
    materialized_refs = tuple(feature_set_refs)
    candidate_by_id = {item.feature_set_id: item for item in materialized_candidates}
    ref_by_id = {str(item.feature_set_id): item for item in materialized_refs}
    if len(candidate_by_id) != len(materialized_candidates):
        raise ValueError("candidate feature-set IDs must be unique")
    if len(ref_by_id) != len(materialized_refs):
        raise ValueError("feature references must be unique")
    if len(candidate_by_id) != len(ref_by_id):
        raise ValueError("candidate and feature reference membership differ")
    if set(candidate_by_id) != set(ref_by_id):
        raise ValueError("candidate and feature reference IDs differ")

    group_partitions: dict[str, DatasetSplit] = {}
    for feature_id, _, split_text, _ in carved.ordered_members:
        candidate = candidate_by_id.get(feature_id)
        if candidate is None:
            raise ValueError("carved membership is absent from supplied inputs")
        split = DatasetSplit(split_text)
        prior = group_partitions.setdefault(candidate.split_group_id, split)
        if prior is not split:
            raise ValueError("one split group cannot cross partitions")
    reconstructed = carve_dataset(
        materialized_candidates,
        group_partitions=group_partitions,
        evaluated_method_id=carved.evaluated_method_id,
    )
    if reconstructed != carved:
        raise ValueError("candidate metadata no longer matches carved snapshot")

    members: list[DatasetMember] = []
    for feature_id, expected_digest, split_text, scored_truth in carved.ordered_members:
        candidate = candidate_by_id.get(feature_id)
        ref = ref_by_id.get(feature_id)
        if candidate is None or ref is None:
            raise ValueError("carved membership is absent from supplied inputs")
        if (
            str(candidate.feature_set_digest) != expected_digest
            or ref.bundle_ref.digest != candidate.feature_set_digest
        ):
            raise ValueError(f"feature digest mismatch for {feature_id}")
        if scored_truth != candidate.scored_truth:
            raise ValueError(f"truth role mismatch for {feature_id}")
        members.append(
            DatasetMember(
                feature_set_ref=ref,
                split_group_id=candidate.split_group_id,
                split=DatasetSplit(split_text),
                role=(
                    DatasetRole.SCORED_TRUTH
                    if scored_truth
                    else DatasetRole.CONTEXT_ONLY
                ),
                truth=candidate.truth,
            )
        )
    if len(members) != len(candidate_by_id):
        raise ValueError("carved membership does not cover supplied inputs")

    ordered_refs = tuple(member.feature_set_ref for member in members)
    model_membership_digest = feature_dataset_membership_digest(ordered_refs)
    provisional_snapshot_id = DatasetSnapshotId("dataset_pending")
    feature_dataset = FeatureDatasetSnapshot(
        schema=SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        snapshot_id=provisional_snapshot_id,
        ordered_feature_set_refs=ordered_refs,
        selection_spec=selection_spec,
        selection_cutoff_utc_ns=selection_cutoff_utc_ns,
        membership_digest=model_membership_digest,
    )
    frozen_members = tuple(members)
    snapshot_digest = dataset_snapshot_digest(
        feature_dataset,
        carved.evaluated_method_id,
        frozen_members,
        carved.promoted,
        carved.diagnostics.warnings,
    )
    feature_dataset = FeatureDatasetSnapshot(
        schema=feature_dataset.schema,
        snapshot_id=DatasetSnapshotId(f"dataset_{snapshot_digest.value[:32]}"),
        ordered_feature_set_refs=feature_dataset.ordered_feature_set_refs,
        selection_spec=feature_dataset.selection_spec,
        selection_cutoff_utc_ns=feature_dataset.selection_cutoff_utc_ns,
        membership_digest=feature_dataset.membership_digest,
    )
    return DatasetSnapshotBundle(
        schema=SchemaRef(DatasetSnapshotBundle.SCHEMA_ID),
        feature_dataset=feature_dataset,
        evaluated_method_id=carved.evaluated_method_id,
        members=frozen_members,
        snapshot_digest=snapshot_digest,
        promoted=carved.promoted,
        promotion_warnings=carved.diagnostics.warnings,
    )


def verify_snapshot_ref(
    snapshot: DatasetSnapshotBundle, expected: DatasetSnapshotRef
) -> None:
    """Reject substitution of either feature membership or truth provenance."""

    if snapshot.ref != expected:
        raise ValueError(
            "dataset reader returned a snapshot that does not match its ref"
        )


def _member_identity(member: DatasetMember) -> object:
    ref = member.feature_set_ref
    return {
        "feature_set_id": str(ref.feature_set_id),
        "analysis_run_id": str(ref.analysis_run_id),
        "bundle_digest": str(ref.bundle_ref.digest),
        "split_group_id": member.split_group_id,
        "split": member.split.value,
        "role": member.role.value,
        "truth": member.truth,
    }
