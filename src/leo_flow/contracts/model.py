"""Frozen dataset and cross-recording model contracts."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    SchemaRef,
    UtcNs,
)
from .ephemeris import EphemerisSnapshotRef
from .features import Covariance, FeatureSetRef
from .hardware import HardwareMetadataSnapshotRef
from .storage import ObjectRef


@dataclass(frozen=True)
class FeatureDatasetSnapshot:
    schema: SchemaRef
    snapshot_id: DatasetSnapshotId
    ordered_feature_set_refs: tuple[FeatureSetRef, ...]
    selection_spec: str
    selection_cutoff_utc_ns: UtcNs
    membership_digest: Digest

    SCHEMA_ID = "org.leo-flow.feature-dataset-snapshot"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported dataset snapshot schema")
        require_utc_ns(self.selection_cutoff_utc_ns, "selection_cutoff_utc_ns")
        if not self.ordered_feature_set_refs:
            raise ValueError("dataset snapshot cannot be empty")
        ids = [ref.feature_set_id for ref in self.ordered_feature_set_refs]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset membership must be unique")
        if self.membership_digest != feature_dataset_membership_digest(
            self.ordered_feature_set_refs
        ):
            raise ValueError(
                "membership_digest does not match ordered feature-set membership"
            )


def feature_dataset_membership_digest(refs: tuple[FeatureSetRef, ...]) -> Digest:
    """Hash stable scientific identity, excluding replaceable blob locators."""
    from .core import canonical_digest

    return canonical_digest(
        [
            {
                "feature_set_id": str(ref.feature_set_id),
                "analysis_run_id": str(ref.analysis_run_id),
                "bundle_digest": str(ref.bundle_ref.digest),
            }
            for ref in refs
        ]
    )


@dataclass(frozen=True)
class FeatureDatasetSnapshotRef:
    snapshot_id: DatasetSnapshotId
    membership_digest: Digest


@dataclass(frozen=True)
class ModelAnalysisRequest:
    schema: SchemaRef
    dataset_snapshot_ref: FeatureDatasetSnapshotRef
    hardware_metadata_snapshot_refs: tuple[HardwareMetadataSnapshotRef, ...]
    ephemeris_snapshot_refs: tuple[EphemerisSnapshotRef, ...]
    model_config_ref: ArtifactRef
    algorithm_ref: ArtifactRef

    SCHEMA_ID = "org.leo-flow.model-analysis-request"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported model analysis request")
        if not self.hardware_metadata_snapshot_refs:
            raise ValueError("model must pin hardware metadata")


@dataclass(frozen=True)
class ParameterEstimate:
    parameter_id: str
    subject_id: str
    value: tuple[float, ...]
    basis: tuple[str, ...]
    units: tuple[str, ...]
    covariance: Covariance

    def __post_init__(self) -> None:
        require_token(self.parameter_id, "parameter_id")
        require_token(self.subject_id, "subject_id")
        if (
            len(self.value) != len(self.basis)
            or self.basis != self.covariance.basis
            or self.units != self.covariance.units
        ):
            raise ValueError("parameter estimate basis, units, and covariance differ")


@dataclass(frozen=True)
class ModelSnapshotBundle:
    schema: SchemaRef
    model_snapshot_id: ModelSnapshotId
    model_run_id: ModelRunId
    dataset_membership_digest: Digest
    hardware_snapshot_digests: tuple[Digest, ...]
    ephemeris_snapshot_digests: tuple[Digest, ...]
    provenance: Provenance
    parameters: tuple[ParameterEstimate, ...]
    warnings: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.model-snapshot-bundle"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported model snapshot schema")


@dataclass(frozen=True)
class ModelSnapshotRef:
    model_snapshot_id: ModelSnapshotId
    model_run_id: ModelRunId
    bundle_ref: ObjectRef


@dataclass(frozen=True)
class ModelApproval:
    approved_by: str
    approved_utc_ns: UtcNs
    rationale: str

    def __post_init__(self) -> None:
        require_token(self.approved_by, "approved_by")
        require_utc_ns(self.approved_utc_ns, "approved_utc_ns")
        if not self.rationale:
            raise ValueError("model approval requires a rationale")


@dataclass(frozen=True)
class ModelRelease:
    alias: str
    model_ref: ModelSnapshotRef
    approval: ModelApproval

    def __post_init__(self) -> None:
        require_token(self.alias, "alias")
