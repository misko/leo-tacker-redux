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
from .dataset_resolution import DatasetResolutionError, resolve_model_dataset
from .nuisance import (
    NuisanceBatchConfig,
    RelativeRadioLnbNuisanceModel,
    nuisance_batch_algorithm_ref,
    nuisance_batch_config_ref,
)
from .simulator import (
    NuisanceSimulationSpec,
    NuisanceTerm,
    ReceiverAssignment,
    simulate_nuisance_observations,
)

__all__ = [
    "DatasetResolutionError",
    "ModelConfigurationError",
    "ModelExecutionContext",
    "ModelInputError",
    "NuisanceBatchConfig",
    "NuisanceSimulationSpec",
    "NuisanceTerm",
    "ReceiverAssignment",
    "ReceiverQualityAggregateConfig",
    "ReceiverQualityAggregateModel",
    "RelativeRadioLnbNuisanceModel",
    "nuisance_batch_algorithm_ref",
    "nuisance_batch_config_ref",
    "receiver_quality_aggregate_algorithm_ref",
    "receiver_quality_aggregate_config_ref",
    "resolve_model_dataset",
    "simulate_nuisance_observations",
]
