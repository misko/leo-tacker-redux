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

__all__ = [
    "DatasetCandidate",
    "DatasetPromotionError",
    "DatasetSnapshot",
    "DatasetSplit",
    "LabelEvidence",
    "LabelSource",
    "MethodAssociationReport",
    "SplitDiagnostics",
    "TruthLabel",
    "carve_dataset",
    "method_firing_association",
]
