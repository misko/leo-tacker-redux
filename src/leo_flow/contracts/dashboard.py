"""Read-only dashboard data transfer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from .capture import ActivityKind
from .core import ModelSnapshotId, RadioId, RecordingId, UtcNs

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class TimeRangeQuery:
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    radio_ids: tuple[RadioId, ...] = ()

    def __post_init__(self) -> None:
        if self.stop_utc_ns <= self.start_utc_ns:
            raise ValueError("dashboard time range is half-open and must be non-empty")


@dataclass(frozen=True)
class RecordingSummary:
    recording_id: RecordingId
    radio_id: RadioId
    started_utc_ns: UtcNs
    finished_utc_ns: UtcNs
    activity_kinds: tuple[ActivityKind, ...]
    analysis_state: str


@dataclass(frozen=True)
class ActivityCount:
    radio_id: RadioId
    kind: ActivityKind
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("activity count cannot be negative")


@dataclass(frozen=True)
class ActivitySummary:
    interval: TimeRangeQuery
    counts: tuple[ActivityCount, ...]


@dataclass(frozen=True)
class RecordingDetail:
    summary: RecordingSummary
    segment_count: int
    recording_object_available: bool


@dataclass(frozen=True)
class FeatureView:
    feature_id: str
    method_id: str
    score: float
    score_semantics: str


@dataclass(frozen=True)
class ModelView:
    model_snapshot_id: ModelSnapshotId
    release_alias: str | None
    parameter_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TrackView:
    track_id: str
    model_snapshot_id: ModelSnapshotId
    started_utc_ns: UtcNs
    finished_utc_ns: UtcNs


@dataclass(frozen=True)
class StorageHealth:
    available: bool
    total_bytes: int | None
    free_bytes: int | None
