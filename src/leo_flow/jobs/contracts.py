"""Fenced at-least-once job lease values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from leo_flow.contracts._validation import (
    freeze_mapping,
    require_positive,
    require_token,
    require_utc_ns,
)
from leo_flow.contracts.core import JobId, SchemaRef, UtcNs


class JobType(str, Enum):
    RECORDING_ANALYSIS = "recording_analysis"
    MODEL_ANALYSIS = "model_analysis"
    EPHEMERIS_RETRIEVAL = "ephemeris_retrieval"
    EPHEMERIS_LINK_BACKFILL = "ephemeris_link_backfill"


@dataclass(frozen=True)
class JobPayload:
    schema: SchemaRef
    value: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        # Re-freezing validates callers that bypass ``create`` without changing
        # the already immutable representation.
        if not isinstance(self.value, tuple):
            raise TypeError("job payload must be an immutable tuple of pairs")
        if tuple(sorted(self.value, key=lambda pair: pair[0])) != self.value:
            raise ValueError("job payload keys must use canonical order")

    @classmethod
    def create(cls, schema: SchemaRef, value: dict[str, Any]) -> JobPayload:
        return cls(schema=schema, value=freeze_mapping(value, "job payload"))


@dataclass(frozen=True)
class JobLease:
    job_id: JobId
    job_type: JobType
    payload: JobPayload
    attempt: int
    lease_token: str
    lease_generation: int
    lease_expires_utc_ns: UtcNs

    def __post_init__(self) -> None:
        require_positive(self.attempt, "attempt")
        require_token(self.lease_token, "lease_token")
        require_positive(self.lease_generation, "lease_generation")
        require_utc_ns(self.lease_expires_utc_ns, "lease_expires_utc_ns")
