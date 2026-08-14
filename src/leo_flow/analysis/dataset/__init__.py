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
from .evaluation import (
    DETECTOR_EVALUATION_FORMAT_ID,
    DETECTOR_EVALUATION_MEDIA_TYPE,
    BinaryClassificationCounts,
    DetectorEvaluationReport,
    MethodEvaluation,
    SplitAssociationReport,
    SplitMethodReport,
    evaluate_detectors,
)
from .evaluation_codec import (
    MAX_DETECTOR_EVALUATION_BYTES,
    MalformedDetectorEvaluationError,
    decode_detector_evaluation,
    encode_detector_evaluation,
)
from .evaluation_persistence import (
    DetectorEvaluationIntegrityError,
    DetectorEvaluationNotFound,
    DurableDetectorEvaluationRepository,
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
    "DETECTOR_EVALUATION_FORMAT_ID",
    "DETECTOR_EVALUATION_MEDIA_TYPE",
    "MAX_DATASET_SNAPSHOT_BYTES",
    "MAX_DETECTOR_EVALUATION_BYTES",
    "BinaryClassificationCounts",
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
    "DetectorEvaluationIntegrityError",
    "DetectorEvaluationNotFound",
    "DetectorEvaluationReport",
    "DurableDatasetSnapshotRepository",
    "DurableDetectorEvaluationRepository",
    "LabelEvidence",
    "LabelSource",
    "MalformedDatasetSnapshotError",
    "MalformedDetectorEvaluationError",
    "MethodAssociationReport",
    "MethodEvaluation",
    "SplitAssociationReport",
    "SplitDiagnostics",
    "SplitMethodReport",
    "TruthLabel",
    "carve_dataset",
    "dataset_snapshot_digest",
    "decode_dataset_snapshot",
    "decode_detector_evaluation",
    "encode_dataset_snapshot",
    "encode_detector_evaluation",
    "evaluate_detectors",
    "freeze_dataset_snapshot",
    "method_firing_association",
    "verify_snapshot_ref",
]
