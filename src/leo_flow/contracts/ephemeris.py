"""Archived ephemeris provenance and temporal-selection contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_nonnegative, require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    SchemaRef,
    UtcNs,
)
from .storage import ObjectRef


class EphemerisSource(str, Enum):
    SPACE_TRACK = "space-track"
    HUGGING_FACE = "huggingface"


class EphemerisSelectionPolicy(str, Enum):
    AVAILABLE_THEN = "available_then"
    FIRST_AFTER = "first_after"
    BEST_EPHEMERIS = "best_ephemeris"


@dataclass(frozen=True)
class RecordingInterval:
    started_utc_ns: UtcNs
    finished_utc_ns: UtcNs

    def __post_init__(self) -> None:
        require_utc_ns(self.started_utc_ns, "started_utc_ns")
        require_utc_ns(self.finished_utc_ns, "finished_utc_ns")
        if self.finished_utc_ns <= self.started_utc_ns:
            raise ValueError("recording interval must be non-empty")


@dataclass(frozen=True)
class EphemerisRetrievalRequest:
    retrieval_id: EphemerisRetrievalId
    source: EphemerisSource
    scope: str
    request_spec: str

    def __post_init__(self) -> None:
        require_token(self.scope, "scope")
        if not self.request_spec:
            raise ValueError("request_spec cannot be empty")


@dataclass(frozen=True)
class RetrievalResult:
    retrieval_id: EphemerisRetrievalId
    source: EphemerisSource
    started_utc_ns: UtcNs
    completed_utc_ns: UtcNs
    raw_object_ref: ObjectRef

    def __post_init__(self) -> None:
        require_utc_ns(self.started_utc_ns, "started_utc_ns")
        require_utc_ns(self.completed_utc_ns, "completed_utc_ns")
        if self.completed_utc_ns < self.started_utc_ns:
            raise ValueError("retrieval completion precedes start")


@dataclass(frozen=True)
class EphemerisSnapshotCandidate:
    source: EphemerisSource
    scope: str
    raw_object_ref: ObjectRef
    normalized_object_ref: ObjectRef
    parser_ref: ArtifactRef
    satellite_count: int
    norad_id_set_digest: Digest
    element_epoch_min_utc_ns: UtcNs
    element_epoch_max_utc_ns: UtcNs
    attribution: str

    def __post_init__(self) -> None:
        require_token(self.scope, "scope")
        require_nonnegative(self.satellite_count, "satellite_count")
        require_utc_ns(self.element_epoch_min_utc_ns, "element_epoch_min_utc_ns")
        require_utc_ns(self.element_epoch_max_utc_ns, "element_epoch_max_utc_ns")
        if self.element_epoch_max_utc_ns < self.element_epoch_min_utc_ns:
            raise ValueError("ephemeris epoch range is inverted")
        if not self.attribution:
            raise ValueError("ephemeris attribution is required")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    policy_ref: ArtifactRef
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EphemerisSnapshot:
    schema: SchemaRef
    snapshot_id: EphemerisSnapshotId
    retrieval_id: EphemerisRetrievalId
    source: EphemerisSource
    scope: str
    retrieved_at_utc_ns: UtcNs
    raw_object_ref: ObjectRef
    normalized_object_ref: ObjectRef
    parser_ref: ArtifactRef
    satellite_count: int
    norad_id_set_digest: Digest
    element_epoch_min_utc_ns: UtcNs
    element_epoch_max_utc_ns: UtcNs
    validation: ValidationResult
    attribution: str

    SCHEMA_ID = "org.leo-flow.ephemeris-snapshot"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported ephemeris snapshot schema")
        require_utc_ns(self.retrieved_at_utc_ns, "retrieved_at_utc_ns")
        require_nonnegative(self.satellite_count, "satellite_count")
        if not self.validation.valid:
            raise ValueError("invalid candidate cannot become an ephemeris snapshot")


@dataclass(frozen=True)
class EphemerisSnapshotRef:
    snapshot_id: EphemerisSnapshotId
    source: EphemerisSource
    raw_digest: Digest
    normalized_digest: Digest


@dataclass(frozen=True)
class EphemerisSelection:
    source: EphemerisSource
    policy: EphemerisSelectionPolicy
    policy_ref: ArtifactRef
    snapshot_ref: EphemerisSnapshotRef
    as_of_utc_ns: UtcNs

    def __post_init__(self) -> None:
        require_utc_ns(self.as_of_utc_ns, "as_of_utc_ns")
        if self.source is not self.snapshot_ref.source:
            raise ValueError("selection cannot cross providers")
