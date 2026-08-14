"""Strict request contract for wider tracking-model analysis."""

from __future__ import annotations

from dataclasses import dataclass

from .core import V0_1, ArtifactRef, SchemaRef
from .tracking_input import TrackingInputSnapshotIdentity


@dataclass(frozen=True)
class TrackingModelAnalysisRequest:
    """One tracking fit over one exact frozen scientific join."""

    schema: SchemaRef
    tracking_input_identity: TrackingInputSnapshotIdentity
    model_config_ref: ArtifactRef
    algorithm_ref: ArtifactRef

    SCHEMA_ID = "org.leo-flow.tracking-model-analysis-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported tracking model analysis request")
        if self.model_config_ref.schema is None or self.algorithm_ref.schema is None:
            raise ValueError("tracking model config and algorithm require schemas")
