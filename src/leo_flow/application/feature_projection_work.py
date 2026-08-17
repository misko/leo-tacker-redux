"""Durable, replay-safe FeatureSet to dashboard projection work.

The work item contains only public immutable identities.  It deliberately does
not carry a decoded bundle or a storage path: a worker must resolve the exact
published recording and open the exact FeatureSet through their public readers
before invoking the existing idempotent projection writer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from leo_flow.application.projection_writers import (
    AnalysisProjectionWriter,
    FeatureProjectionCommand,
    ProjectionConflict,
)
from leo_flow.application.projections import ProjectionInputError
from leo_flow.contracts._validation import (
    require_positive,
    require_token,
    require_utc_ns,
)
from leo_flow.contracts.core import Digest, JobId, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.ports import FeatureSetReader
from leo_flow.contracts.storage import PublishedRecordingRef

FEATURE_PROJECTION_WORK_SCHEMA = SchemaRef("org.leo-flow.feature-projection-work")
_REASON_CODE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")


class FeatureProjectionWorkError(RuntimeError):
    """Base error for the dedicated projection-work state machine."""


class StaleFeatureProjectionLeaseError(FeatureProjectionWorkError):
    """A transition did not own the current live lease generation."""


class FeatureProjectionIdentityError(FeatureProjectionWorkError):
    """Durable work no longer resolves its exact public identities."""


@dataclass(frozen=True)
class FeatureProjectionWork:
    """Versioned immutable input to one FeatureSet projection."""

    schema: SchemaRef
    work_id: str
    source_job_id: JobId
    feature_set_ref: FeatureSetRef
    recording_id: RecordingId
    recording_identity_digest: Digest

    def __post_init__(self) -> None:
        if self.schema != FEATURE_PROJECTION_WORK_SCHEMA:
            raise ValueError("unsupported feature projection work schema")
        require_token(self.work_id, "work_id")
        if not self.work_id.startswith("fpwork_"):
            raise ValueError("work_id must start with 'fpwork_'")


@dataclass(frozen=True)
class FeatureProjectionWorkLease:
    work: FeatureProjectionWork
    attempt: int
    lease_token: str
    lease_generation: int
    lease_expires_utc_ns: UtcNs

    def __post_init__(self) -> None:
        require_positive(self.attempt, "attempt")
        require_token(self.lease_token, "lease_token")
        require_positive(self.lease_generation, "lease_generation")
        require_utc_ns(self.lease_expires_utc_ns, "lease_expires_utc_ns")


class FeatureProjectionWorkRepository(Protocol):
    """Narrow leased work port, separate from the generic analysis job API."""

    def claim(
        self, worker_id: str, ttl_s: float
    ) -> FeatureProjectionWorkLease | None: ...

    def heartbeat(
        self,
        work_id: str,
        lease_token: str,
        generation: int,
        ttl_s: float,
    ) -> FeatureProjectionWorkLease: ...

    def complete(self, work_id: str, lease_token: str, generation: int) -> None: ...

    def retry(
        self,
        work_id: str,
        lease_token: str,
        generation: int,
        reason: str,
        delay_s: float,
    ) -> None: ...

    def park(
        self, work_id: str, lease_token: str, generation: int, reason: str
    ) -> None: ...


class PublishedRecordingReader(Protocol):
    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


class FeatureProjectionWorker:
    """Resolve exact public artifacts and project one leased work item."""

    def __init__(
        self,
        work: FeatureProjectionWorkRepository,
        features: FeatureSetReader,
        recordings: PublishedRecordingReader,
        writer: AnalysisProjectionWriter,
        *,
        worker_id: str,
        lease_ttl_s: float,
        retry_delay_s: float,
        maximum_attempts: int = 3,
    ) -> None:
        require_token(worker_id, "worker_id")
        require_positive(lease_ttl_s, "lease_ttl_s")
        require_positive(retry_delay_s, "retry_delay_s")
        require_positive(maximum_attempts, "maximum_attempts")
        self._work = work
        self._features = features
        self._recordings = recordings
        self._writer = writer
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._retry_delay_s = retry_delay_s
        self._maximum_attempts = maximum_attempts

    def process_one_work(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        self.execute(lease)
        return True

    def execute(self, lease: FeatureProjectionWorkLease) -> None:
        """Project once; exact replay converges if completion was interrupted."""

        try:
            item = lease.work
            recording = self._recordings.get(item.recording_id)
            if recording is None or (
                recording.recording_object.identity_digest()
                != item.recording_identity_digest
            ):
                raise FeatureProjectionIdentityError(
                    "projection work recording identity is not authoritative"
                )
            with self._features.open(item.feature_set_ref) as view:
                if view.ref != item.feature_set_ref:
                    raise FeatureProjectionIdentityError(
                        "feature reader returned a different public reference"
                    )
                bundle = view.bundle()
            self._writer.project_features(
                FeatureProjectionCommand(bundle, item.feature_set_ref, recording)
            )
            self._work.complete(item.work_id, lease.lease_token, lease.lease_generation)
        except StaleFeatureProjectionLeaseError:
            raise
        except (
            FeatureProjectionIdentityError,
            ProjectionConflict,
            ProjectionInputError,
        ):
            self._park_if_current(lease, "projection_identity_mismatch")
        except Exception:
            if lease.attempt >= self._maximum_attempts:
                self._park_if_current(lease, "projection_attempts_exhausted")
                return
            try:
                self._work.retry(
                    lease.work.work_id,
                    lease.lease_token,
                    lease.lease_generation,
                    "projection_transient_failure",
                    self._retry_delay_s,
                )
            except StaleFeatureProjectionLeaseError:
                pass
            raise

    def _park_if_current(self, lease: FeatureProjectionWorkLease, reason: str) -> None:
        if _REASON_CODE.fullmatch(reason) is None:  # pragma: no cover - constants
            raise ValueError("invalid projection parking reason")
        try:
            self._work.park(
                lease.work.work_id,
                lease.lease_token,
                lease.lease_generation,
                reason,
            )
        except StaleFeatureProjectionLeaseError:
            pass
