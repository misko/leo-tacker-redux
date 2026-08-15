"""Versioned label evidence and leakage-safe dataset partition contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from .features import FeatureSetRef


class EvidenceKind(str, Enum):
    CONTROLLED_INJECTION_TRUTH = "controlled_injection_truth"
    INDEPENDENT_VERIFIED_OBSERVATION = "independent_verified_observation"
    TLE_WEAK_ASSOCIATION = "tle_weak_association"
    OPERATOR_NOTE = "operator_note"
    VERIFIED_NEGATIVE_CONTROL = "verified_negative_control"
    UNLABELED = "unlabeled"


class EvidencePartition(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    LOCKED_TEST = "locked_test"


class EvidenceRole(str, Enum):
    SCORED_TRUTH = "scored_truth"
    CONTEXT_ONLY = "context_only"


@dataclass(frozen=True)
class LabelEvidenceRef:
    """One immutable, independently attributable evidence object."""

    schema: SchemaRef
    evidence_id: str
    kind: EvidenceKind
    artifact_ref: ArtifactRef
    producer_id: str
    produced_utc_ns: UtcNs
    independent_of_method_ids: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.label-evidence-ref"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported label evidence schema")
        require_token(self.evidence_id, "evidence_id")
        require_token(self.producer_id, "producer_id")
        require_utc_ns(self.produced_utc_ns, "produced_utc_ns")
        if self.kind is EvidenceKind.UNLABELED:
            raise ValueError("unlabeled data cannot cite label evidence")
        if self.artifact_ref.schema is None:
            raise ValueError("label evidence artifact must be versioned")
        methods = self.independent_of_method_ids
        if (
            len(methods) != len(set(methods))
            or methods != tuple(sorted(methods))
            or any(not method for method in methods)
        ):
            raise ValueError("independent method IDs must be unique and canonical")


@dataclass(frozen=True)
class ObservationLabel:
    """A label category that never upgrades weak evidence into ground truth."""

    schema: SchemaRef
    label_id: str
    recording_id: RecordingId
    kind: EvidenceKind
    target_present: bool | None
    evidence_refs: tuple[LabelEvidenceRef, ...]
    base_recording_digest: Digest | None = None
    injection_spec_digest: Digest | None = None

    SCHEMA_ID = "org.leo-flow.observation-label"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported observation label schema")
        require_token(self.label_id, "label_id")
        if self.kind is EvidenceKind.UNLABELED:
            if self.target_present is not None or self.evidence_refs:
                raise ValueError("unlabeled data cannot assert or cite a label")
        else:
            if self.target_present is None or not self.evidence_refs:
                raise ValueError("labeled data must assert a target and cite evidence")
            if any(item.kind is not self.kind for item in self.evidence_refs):
                raise ValueError("label and evidence kinds differ")
            ids = tuple(item.evidence_id for item in self.evidence_refs)
            if len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
                raise ValueError("label evidence must be unique and canonical")
        injection_lineage = (
            self.base_recording_digest is not None
            and self.injection_spec_digest is not None
        )
        if self.kind is EvidenceKind.CONTROLLED_INJECTION_TRUTH:
            if self.target_present is not True or not injection_lineage:
                raise ValueError(
                    "controlled injection truth requires positive exact lineage"
                )
        elif (
            self.base_recording_digest is not None
            or self.injection_spec_digest is not None
        ):
            raise ValueError(
                "only controlled injection truth carries injection lineage"
            )
        if (
            self.kind is EvidenceKind.VERIFIED_NEGATIVE_CONTROL
            and self.target_present is not False
        ):
            raise ValueError("verified negative control must be negative")

    def usable_as_truth_for(self, method_id: str) -> bool:
        require_token(method_id, "method_id")
        if self.kind not in {
            EvidenceKind.CONTROLLED_INJECTION_TRUTH,
            EvidenceKind.INDEPENDENT_VERIFIED_OBSERVATION,
            EvidenceKind.VERIFIED_NEGATIVE_CONTROL,
        }:
            return False
        return all(
            method_id in evidence.independent_of_method_ids
            for evidence in self.evidence_refs
        )


@dataclass(frozen=True)
class EvidencePartitionMember:
    feature_set_ref: FeatureSetRef
    recording_id: RecordingId
    recording_identity_digest: Digest
    leakage_group_ids: tuple[str, ...]
    partition: EvidencePartition
    role: EvidenceRole
    label: ObservationLabel

    def __post_init__(self) -> None:
        if self.recording_id != self.label.recording_id:
            raise ValueError("partition member and label recording IDs differ")
        groups = self.leakage_group_ids
        if (
            not groups
            or len(groups) != len(set(groups))
            or groups != tuple(sorted(groups))
        ):
            raise ValueError("leakage groups must be non-empty, unique, and canonical")
        for group_id in groups:
            require_token(group_id, "leakage_group_id")


@dataclass(frozen=True)
class EvidencePartitionPlan:
    schema: SchemaRef
    evaluated_method_id: str
    members: tuple[EvidencePartitionMember, ...]
    partition_digest: Digest

    SCHEMA_ID = "org.leo-flow.evidence-partition-plan"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported evidence partition schema")
        require_token(self.evaluated_method_id, "evaluated_method_id")
        if not self.members:
            raise ValueError("evidence partition cannot be empty")
        _validate_members(self.members, self.evaluated_method_id)
        if self.partition_digest != evidence_partition_digest(
            self.evaluated_method_id, self.members
        ):
            raise ValueError("partition digest does not match its members")

    @classmethod
    def create(
        cls,
        evaluated_method_id: str,
        members: tuple[EvidencePartitionMember, ...],
    ) -> EvidencePartitionPlan:
        order = {
            EvidencePartition.TRAIN: 0,
            EvidencePartition.VALIDATION: 1,
            EvidencePartition.LOCKED_TEST: 2,
        }
        canonical = tuple(
            sorted(
                members,
                key=lambda item: (
                    order[item.partition],
                    item.leakage_group_ids,
                    str(item.feature_set_ref.feature_set_id),
                ),
            )
        )
        return cls(
            SchemaRef(cls.SCHEMA_ID, V0_1),
            evaluated_method_id,
            canonical,
            evidence_partition_digest(evaluated_method_id, canonical),
        )


def evidence_partition_digest(
    evaluated_method_id: str, members: tuple[EvidencePartitionMember, ...]
) -> Digest:
    """Hash partition and truth identity while excluding replaceable locators."""

    return canonical_digest(
        {
            "evaluated_method_id": evaluated_method_id,
            "members": tuple(_member_identity(member) for member in members),
        }
    )


def _validate_members(
    members: tuple[EvidencePartitionMember, ...], evaluated_method_id: str
) -> None:
    feature_ids = tuple(str(item.feature_set_ref.feature_set_id) for item in members)
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError("partition FeatureSet IDs must be unique")
    partition_by_leakage_key: dict[str, EvidencePartition] = {}
    for member in members:
        if (
            member.role is EvidenceRole.SCORED_TRUTH
            and not member.label.usable_as_truth_for(evaluated_method_id)
        ):
            raise ValueError("scored labels must be independent strong truth")
        implicit_keys = [f"recording:{member.recording_identity_digest}"]
        if member.label.base_recording_digest is not None:
            implicit_keys.append(f"recording:{member.label.base_recording_digest}")
        for key in (*member.leakage_group_ids, *implicit_keys):
            prior = partition_by_leakage_key.setdefault(key, member.partition)
            if prior is not member.partition:
                raise ValueError("one leakage group cannot cross partitions")


def _member_identity(member: EvidencePartitionMember) -> object:
    ref = member.feature_set_ref
    return {
        "feature_set_id": str(ref.feature_set_id),
        "analysis_run_id": str(ref.analysis_run_id),
        "bundle_digest": str(ref.bundle_ref.digest),
        "recording_id": str(member.recording_id),
        "recording_identity_digest": str(member.recording_identity_digest),
        "leakage_group_ids": member.leakage_group_ids,
        "partition": member.partition.value,
        "role": member.role.value,
        "label": member.label,
    }
