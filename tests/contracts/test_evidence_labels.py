from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureSetId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.evidence import (
    EvidenceKind,
    EvidencePartition,
    EvidencePartitionMember,
    EvidencePartitionPlan,
    EvidenceRole,
    LabelEvidenceRef,
    ObservationLabel,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef

METHOD = "detector_v1"


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _evidence(kind: EvidenceKind, suffix: str) -> LabelEvidenceRef:
    return LabelEvidenceRef(
        SchemaRef(LabelEvidenceRef.SCHEMA_ID),
        f"evidence_{suffix}",
        kind,
        ArtifactRef(
            f"artifact_{suffix}",
            _digest(f"artifact:{suffix}"),
            SchemaRef(f"org.leo-flow.evidence.{suffix}"),
        ),
        f"producer_{suffix}",
        UtcNs(100),
        (METHOD,),
    )


def _label(kind: EvidenceKind, suffix: str) -> ObservationLabel:
    if kind is EvidenceKind.UNLABELED:
        return ObservationLabel(
            SchemaRef(ObservationLabel.SCHEMA_ID),
            f"label_{suffix}",
            RecordingId(f"rec_{suffix}"),
            kind,
            None,
            (),
        )
    return ObservationLabel(
        SchemaRef(ObservationLabel.SCHEMA_ID),
        f"label_{suffix}",
        RecordingId(f"rec_{suffix}"),
        kind,
        kind is not EvidenceKind.VERIFIED_NEGATIVE_CONTROL,
        (_evidence(kind, suffix),),
        base_recording_digest=(
            _digest(f"base:{suffix}")
            if kind is EvidenceKind.CONTROLLED_INJECTION_TRUTH
            else None
        ),
        injection_spec_digest=(
            _digest(f"injection:{suffix}")
            if kind is EvidenceKind.CONTROLLED_INJECTION_TRUTH
            else None
        ),
    )


def _feature(index: int) -> FeatureSetRef:
    payload = f"feature:{index}".encode()
    return FeatureSetRef(
        FeatureSetId(f"fset_evidence_{index}"),
        AnalysisRunId(f"arun_evidence_{index}"),
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            "application/json",
            "feature-set-bundle-v0.1",
            f"memory:feature:{index}",
        ),
    )


def _member(
    index: int,
    label: ObservationLabel,
    partition: EvidencePartition,
    *,
    group: str,
    role: EvidenceRole = EvidenceRole.CONTEXT_ONLY,
    recording_digest: Digest | None = None,
) -> EvidencePartitionMember:
    return EvidencePartitionMember(
        _feature(index),
        label.recording_id,
        recording_digest or _digest(f"recording:{index}"),
        (group,),
        partition,
        role,
        label,
    )


def test_label_kinds_do_not_upgrade_weak_sources_to_truth() -> None:
    truth_kinds = {
        EvidenceKind.CONTROLLED_INJECTION_TRUTH,
        EvidenceKind.INDEPENDENT_VERIFIED_OBSERVATION,
        EvidenceKind.VERIFIED_NEGATIVE_CONTROL,
    }

    for index, kind in enumerate(EvidenceKind):
        label = _label(kind, f"kind_{index}")
        assert label.usable_as_truth_for(METHOD) is (kind in truth_kinds)


def test_labels_reject_unknown_versions_and_invalid_category_semantics() -> None:
    label = _label(EvidenceKind.INDEPENDENT_VERIFIED_OBSERVATION, "observed")
    with pytest.raises(ValueError, match="unsupported"):
        replace(
            label,
            schema=SchemaRef(label.SCHEMA_ID, SchemaVersion(1, 0)),
        )
    with pytest.raises(ValueError, match="negative"):
        replace(
            _label(EvidenceKind.VERIFIED_NEGATIVE_CONTROL, "negative"),
            target_present=True,
        )
    with pytest.raises(ValueError, match="unlabeled"):
        replace(_label(EvidenceKind.UNLABELED, "unknown"), target_present=False)


def test_partition_is_deterministic_and_rejects_scored_weak_evidence() -> None:
    first = _member(
        1,
        _label(EvidenceKind.INDEPENDENT_VERIFIED_OBSERVATION, "one"),
        EvidencePartition.TRAIN,
        group="group_one",
        role=EvidenceRole.SCORED_TRUTH,
    )
    second = _member(
        2,
        _label(EvidenceKind.TLE_WEAK_ASSOCIATION, "two"),
        EvidencePartition.VALIDATION,
        group="group_two",
    )

    assert EvidencePartitionPlan.create(METHOD, (first, second)) == (
        EvidencePartitionPlan.create(METHOD, (second, first))
    )
    with pytest.raises(ValueError, match="strong truth"):
        EvidencePartitionPlan.create(
            METHOD, (first, replace(second, role=EvidenceRole.SCORED_TRUTH))
        )


def test_explicit_groups_and_injection_base_lineage_cannot_cross_partitions() -> None:
    observed = _label(EvidenceKind.INDEPENDENT_VERIFIED_OBSERVATION, "base")
    base_digest = _digest("shared-base-recording")
    base = _member(
        1,
        observed,
        EvidencePartition.TRAIN,
        group="session_base",
        recording_digest=base_digest,
    )
    injected_label = replace(
        _label(EvidenceKind.CONTROLLED_INJECTION_TRUTH, "injected"),
        base_recording_digest=base_digest,
    )
    injected = _member(
        2,
        injected_label,
        EvidencePartition.VALIDATION,
        group="session_injected",
    )
    with pytest.raises(ValueError, match="leakage group"):
        EvidencePartitionPlan.create(METHOD, (base, injected))

    shared_group = replace(injected, leakage_group_ids=("session_base",))
    with pytest.raises(ValueError, match="leakage group"):
        EvidencePartitionPlan.create(METHOD, (base, shared_group))
