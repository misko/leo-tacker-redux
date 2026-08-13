"""Read-only dashboard projection queries and framework-neutral JSON API."""

from .api import DashboardJsonApplication, JsonRequest, JsonResponse
from .repository import (
    ActivityProjection,
    DashboardNotFound,
    FeatureProjection,
    InMemoryDashboardRepository,
    ModelProjection,
    RecordingProjection,
    TrackProjection,
)

__all__ = [
    "ActivityProjection",
    "DashboardJsonApplication",
    "DashboardNotFound",
    "FeatureProjection",
    "InMemoryDashboardRepository",
    "JsonRequest",
    "JsonResponse",
    "ModelProjection",
    "RecordingProjection",
    "TrackProjection",
]
