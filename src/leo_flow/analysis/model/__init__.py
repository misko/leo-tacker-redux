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
from .inputs import (
    AssembledModelInputs,
    EphemerisLinkRequirement,
    ModelInputAssemblyError,
    assemble_model_inputs,
)
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
from .tracking_fitter import (
    ExactTrackingSourceInputs,
    ExperimentalFixedNoradTrackingConfig,
    ExperimentalFixedNoradTrackingModel,
    ExtractedTrackingInput,
    RfFeatureSelector,
    TrackingInputExtractionError,
    TrackingInputExtractor,
    UnsupportedV01FeatureTrackingExtractor,
    experimental_tracking_algorithm_ref,
    experimental_tracking_config_ref,
)

__all__ = [
    "AssembledModelInputs",
    "DatasetResolutionError",
    "DurableModelSnapshotRepository",
    "EphemerisLinkRequirement",
    "ExactTrackingSourceInputs",
    "ExperimentalFixedNoradTrackingConfig",
    "ExperimentalFixedNoradTrackingModel",
    "ExtractedTrackingInput",
    "MalformedModelSnapshotError",
    "ModelConfigurationError",
    "ModelExecutionContext",
    "ModelInputAssemblyError",
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
    "RfFeatureSelector",
    "TrackingInputExtractionError",
    "TrackingInputExtractor",
    "UnsupportedV01FeatureTrackingExtractor",
    "assemble_model_inputs",
    "decode_model_snapshot",
    "encode_model_snapshot",
    "experimental_tracking_algorithm_ref",
    "experimental_tracking_config_ref",
    "nuisance_batch_algorithm_ref",
    "nuisance_batch_config_ref",
    "receiver_quality_aggregate_algorithm_ref",
    "receiver_quality_aggregate_config_ref",
    "resolve_model_dataset",
    "simulate_nuisance_observations",
]
