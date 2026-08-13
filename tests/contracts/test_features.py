from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    AnalysisRunId,
    DatasetSnapshotId,
    FeatureId,
    FeatureSetId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.features import Covariance, FeatureObservation, FeatureSetRef
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    feature_dataset_membership_digest,
)
from testkit import object_ref


def covariance() -> Covariance:
    return Covariance(
        basis=("frequency_hz", "drift_hz_s"),
        units=("Hz", "Hz/s"),
        values=((4.0, 1.0), (1.0, 2.0)),
    )


def test_covariance_declares_basis_units_symmetry_and_psd() -> None:
    assert covariance().basis[0] == "frequency_hz"
    with pytest.raises(ValueError, match="symmetric"):
        Covariance(("a", "b"), ("1", "1"), ((1.0, 0.5), (0.0, 1.0)))
    with pytest.raises(ValueError, match="positive semidefinite"):
        Covariance(("a", "b"), ("1", "1"), ((1.0, 2.0), (2.0, 1.0)))


def test_observation_window_must_lie_inside_segment() -> None:
    observation = FeatureObservation(
        FeatureId("feature_01"),
        RecordingId("rec_01"),
        SegmentId("seg_01"),
        "diff",
        "1.0",
        10,
        20,
        100,
        1_700_000_000_000_000_000,
        "periodicity",
        2.3,
        "higher_is_more_signal",
        receiver_chain_id=ReceiverChainId("rx_a"),
        covariance=covariance(),
    )
    assert observation.window_stop_sample == 20
    with pytest.raises(ValueError, match="outside"):
        replace(observation, window_stop_sample=101)
    with pytest.raises(ValueError, match="exactly one"):
        replace(observation, receiver_chain_id=None)


def test_dataset_digest_pins_order_and_excludes_locator() -> None:
    a = FeatureSetRef(
        FeatureSetId("fset_01"), AnalysisRunId("arun_01"), object_ref("same")
    )
    moved = replace(a, bundle_ref=replace(a.bundle_ref, locator="opaque:moved"))
    assert feature_dataset_membership_digest((a,)) == feature_dataset_membership_digest(
        (moved,)
    )
    snapshot = FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_01"),
        (a,),
        "published before cutoff",
        1_700_000_000_000_000_000,
        feature_dataset_membership_digest((a,)),
    )
    assert snapshot.ordered_feature_set_refs == (a,)
    with pytest.raises(ValueError, match="membership_digest"):
        replace(snapshot, membership_digest=object_ref("wrong").digest)
