"""Deterministic cross-recording models over frozen feature datasets."""

from .api import (
    ModelConfigurationError,
    ModelExecutionContext,
    ModelInputError,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
    receiver_quality_aggregate_algorithm_ref,
    receiver_quality_aggregate_config_ref,
)
from .codec import (
    MalformedModelSnapshotError,
    decode_model_snapshot,
    encode_model_snapshot,
)
from .dataset_resolution import DatasetResolutionError, resolve_model_dataset
from .nuisance import (
    NuisanceBatchConfig,
    RelativeRadioLnbNuisanceModel,
    nuisance_batch_algorithm_ref,
    nuisance_batch_config_ref,
)
from .persistence import (
    DurableModelSnapshotRepository,
    ModelSnapshotIntegrityError,
    ModelSnapshotNotFoundError,
)
from .simulator import (
    NuisanceSimulationSpec,
    NuisanceTerm,
    ReceiverAssignment,
    simulate_nuisance_observations,
)

__all__ = [
    "DatasetResolutionError",
    "DurableModelSnapshotRepository",
    "MalformedModelSnapshotError",
    "ModelConfigurationError",
    "ModelExecutionContext",
    "ModelInputError",
    "ModelSnapshotIntegrityError",
    "ModelSnapshotNotFoundError",
    "NuisanceBatchConfig",
    "NuisanceSimulationSpec",
    "NuisanceTerm",
    "ReceiverAssignment",
    "ReceiverQualityAggregateConfig",
    "ReceiverQualityAggregateModel",
    "RelativeRadioLnbNuisanceModel",
    "decode_model_snapshot",
    "encode_model_snapshot",
    "nuisance_batch_algorithm_ref",
    "nuisance_batch_config_ref",
    "receiver_quality_aggregate_algorithm_ref",
    "receiver_quality_aggregate_config_ref",
    "resolve_model_dataset",
    "simulate_nuisance_observations",
]
