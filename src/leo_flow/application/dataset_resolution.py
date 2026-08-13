"""Resolve one rich dataset snapshot into the narrower model-facing contract."""

from __future__ import annotations

from leo_flow.analysis.dataset import (
    DatasetSnapshotReader,
    DatasetSnapshotRef,
    verify_snapshot_ref,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
)


class DatasetResolutionError(ValueError):
    """A model request does not identify the exact durable dataset supplied."""


def resolve_model_dataset(
    reader: DatasetSnapshotReader,
    durable_ref: DatasetSnapshotRef,
    model_ref: FeatureDatasetSnapshotRef,
) -> FeatureDatasetSnapshot:
    """Verify rich identity before exposing only frozen feature membership.

    This composition seam prevents a model worker from accepting a matching
    feature list whose split, truth, role, or promotion provenance was replaced.
    The model receives no dataset persistence capability and cannot inspect a
    locked-test label through this function.
    """

    snapshot = reader.get(durable_ref)
    try:
        verify_snapshot_ref(snapshot, durable_ref)
    except ValueError as error:
        raise DatasetResolutionError("dataset reader substituted snapshot") from error
    expected = FeatureDatasetSnapshotRef(
        snapshot.feature_dataset.snapshot_id,
        snapshot.feature_dataset.membership_digest,
    )
    if model_ref != expected:
        raise DatasetResolutionError(
            "model request does not match durable dataset membership"
        )
    return snapshot.feature_dataset
