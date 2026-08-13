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

__all__ = [
    "DatasetResolutionError",
    "ModelConfigurationError",
    "ModelExecutionContext",
    "ModelInputError",
    "ReceiverQualityAggregateConfig",
    "ReceiverQualityAggregateModel",
    "receiver_quality_aggregate_algorithm_ref",
    "receiver_quality_aggregate_config_ref",
    "resolve_model_dataset",
]
