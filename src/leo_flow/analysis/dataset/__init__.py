"""Leakage-resistant dataset construction and method comparison."""

from .api import (
    DatasetCandidate,
    DatasetPromotionError,
    DatasetSnapshot,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    SplitDiagnostics,
    TruthLabel,
    carve_dataset,
)
from .association import MethodAssociationReport, method_firing_association
from .codec import (
    MAX_DATASET_SNAPSHOT_BYTES,
    MalformedDatasetSnapshotError,
    decode_dataset_snapshot,
    encode_dataset_snapshot,
)
from .persistence import (
    DATASET_SNAPSHOT_FORMAT_ID,
    DATASET_SNAPSHOT_MEDIA_TYPE,
    DatasetSnapshotIntegrityError,
    DatasetSnapshotNotFoundError,
    DatasetSnapshotPersistenceError,
    DurableDatasetSnapshotRepository,
)
from .ports import DatasetSnapshotPublisher, DatasetSnapshotReader
from .snapshot import (
    DatasetMember,
    DatasetRole,
    DatasetSnapshotBundle,
    DatasetSnapshotRef,
    dataset_snapshot_digest,
    freeze_dataset_snapshot,
    verify_snapshot_ref,
)

__all__ = [
    "DATASET_SNAPSHOT_FORMAT_ID",
    "DATASET_SNAPSHOT_MEDIA_TYPE",
    "MAX_DATASET_SNAPSHOT_BYTES",
    "DatasetCandidate",
    "DatasetMember",
    "DatasetPromotionError",
    "DatasetRole",
    "DatasetSnapshot",
    "DatasetSnapshotBundle",
    "DatasetSnapshotIntegrityError",
    "DatasetSnapshotNotFoundError",
    "DatasetSnapshotPersistenceError",
    "DatasetSnapshotPublisher",
    "DatasetSnapshotReader",
    "DatasetSnapshotRef",
    "DatasetSplit",
    "DurableDatasetSnapshotRepository",
    "LabelEvidence",
    "LabelSource",
    "MalformedDatasetSnapshotError",
    "MethodAssociationReport",
    "SplitDiagnostics",
    "TruthLabel",
    "carve_dataset",
    "dataset_snapshot_digest",
    "decode_dataset_snapshot",
    "encode_dataset_snapshot",
    "freeze_dataset_snapshot",
    "method_firing_association",
    "verify_snapshot_ref",
]
