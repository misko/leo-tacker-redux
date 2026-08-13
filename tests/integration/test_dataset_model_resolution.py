from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetMember,
    DatasetRole,
    DatasetSnapshotBundle,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    dataset_snapshot_digest,
)
from leo_flow.application import DatasetResolutionError, resolve_model_dataset
from leo_flow.contracts.core import (
    AnalysisRunId,
    DatasetSnapshotId,
    Digest,
    FeatureSetId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    feature_dataset_membership_digest,
)
from leo_flow.contracts.storage import ObjectRef


class SnapshotReader:
    def __init__(self, returned: DatasetSnapshotBundle) -> None:
        self.returned = returned
        self.calls = []

    def get(self, ref):
        self.calls.append(ref)
        return self.returned


def snapshot() -> DatasetSnapshotBundle:
    feature_ref = FeatureSetRef(
        FeatureSetId("fset_resolution"),
        AnalysisRunId("arun_resolution"),
        ObjectRef(
            Digest.sha256(b"features"),
            128,
            "application/json",
            "feature-set-json-v0.1",
            "opaque://features/resolution",
        ),
    )
    feature_dataset = FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_resolution"),
        (feature_ref,),
        "reviewed-groups:v1",
        UtcNs(10_000),
        feature_dataset_membership_digest((feature_ref,)),
    )
    truth = TruthLabel(
        True,
        LabelSource.OBSERVED,
        (
            LabelEvidence(
                LabelSource.OBSERVED,
                Digest.sha256(b"independent-observation"),
                "independent-review-v1",
                9_000,
                ("method-a",),
            ),
        ),
        1.0,
    )
    members = (
        DatasetMember(
            feature_ref,
            "pass-a",
            DatasetSplit.TRAIN,
            DatasetRole.SCORED_TRUTH,
            truth,
        ),
    )
    digest = dataset_snapshot_digest(feature_dataset, "method-a", members, True, ())
    return DatasetSnapshotBundle(
        SchemaRef(DatasetSnapshotBundle.SCHEMA_ID),
        feature_dataset,
        "method-a",
        members,
        digest,
        True,
        (),
    )


def test_rich_snapshot_is_verified_before_model_receives_feature_membership() -> None:
    frozen = snapshot()
    reader = SnapshotReader(frozen)
    model_ref = FeatureDatasetSnapshotRef(
        frozen.feature_dataset.snapshot_id,
        frozen.feature_dataset.membership_digest,
    )

    resolved = resolve_model_dataset(reader, frozen.ref, model_ref)

    assert resolved is frozen.feature_dataset
    assert reader.calls == [frozen.ref]


def test_truth_provenance_substitution_is_rejected_before_model_access() -> None:
    expected = snapshot()
    changed_truth = replace(expected.members[0].truth, confidence=0.5)
    changed_members = (replace(expected.members[0], truth=changed_truth),)
    substituted = replace(
        expected,
        members=changed_members,
        snapshot_digest=dataset_snapshot_digest(
            expected.feature_dataset,
            expected.evaluated_method_id,
            changed_members,
            expected.promoted,
            expected.promotion_warnings,
        ),
    )
    model_ref = FeatureDatasetSnapshotRef(
        expected.feature_dataset.snapshot_id,
        expected.feature_dataset.membership_digest,
    )

    with pytest.raises(DatasetResolutionError, match="substituted"):
        resolve_model_dataset(SnapshotReader(substituted), expected.ref, model_ref)


def test_model_membership_ref_must_match_verified_durable_snapshot() -> None:
    frozen = snapshot()
    wrong = FeatureDatasetSnapshotRef(
        frozen.feature_dataset.snapshot_id,
        Digest.sha256(b"wrong-membership"),
    )

    with pytest.raises(DatasetResolutionError, match="does not match"):
        resolve_model_dataset(SnapshotReader(frozen), frozen.ref, wrong)
