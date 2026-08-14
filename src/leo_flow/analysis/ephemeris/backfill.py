"""Typed preparation boundary for recording-to-ephemeris link backfills."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, cast

from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.contracts.storage import PublishedRecordingRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.storage.ports import RecordingObjectReader

EPHEMERIS_LINK_BACKFILL_JOB_SCHEMA = SchemaRef(
    "org.leo-flow.ephemeris-link-backfill-job"
)


class EphemerisLinkBackfillError(ValueError):
    pass


@dataclass(frozen=True)
class EphemerisLinkRequest:
    recording_id: RecordingId
    source: EphemerisSource
    scope: str
    policy: EphemerisSelectionPolicy
    policy_ref: ArtifactRef
    as_of_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if not self.scope or any(character.isspace() for character in self.scope):
            raise ValueError("scope must be a token")
        if self.policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
            raise ValueError("best_ephemeris has no frozen selection semantics")


@dataclass(frozen=True)
class PreparedEphemerisLink:
    request: EphemerisLinkRequest
    recording_ref: RecordingObjectRef
    recording_interval: RecordingInterval


class RecordingCatalogReader(Protocol):
    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


class EphemerisLinkCommitter(Protocol):
    def commit(
        self, lease: JobLease, prepared: PreparedEphemerisLink
    ) -> ArtifactRef: ...


class EphemerisLinkBackfillPreparer:
    """Read the exact published manifest; never reads provider credentials."""

    def __init__(
        self, recordings: RecordingCatalogReader, reader: RecordingObjectReader
    ) -> None:
        self._recordings = recordings
        self._reader = reader

    def prepare(self, lease: JobLease) -> PreparedEphemerisLink:
        if lease.job_type is not JobType.EPHEMERIS_LINK_BACKFILL:
            raise EphemerisLinkBackfillError("worker accepts link-backfill jobs only")
        request = decode_ephemeris_link_payload(lease.payload)
        published = self._recordings.get(request.recording_id)
        if published is None:
            raise EphemerisLinkBackfillError(
                "recording is not authoritatively published"
            )
        ref = published.recording_object
        with self._reader.open(ref) as view:
            manifest = view.manifest
        if manifest.recording_id != request.recording_id:
            raise EphemerisLinkBackfillError("recording reader substituted manifest")
        interval = RecordingInterval(
            manifest.capture_started_utc_ns, manifest.capture_finished_utc_ns
        )
        return PreparedEphemerisLink(request, ref, interval)


class EphemerisLinkBackfillExecutor:
    """Execute one claimed lease and leave failures retryable under its fence."""

    def __init__(
        self,
        jobs: JobLeaseRepository,
        preparer: EphemerisLinkBackfillPreparer,
        committer: EphemerisLinkCommitter,
        *,
        retry_delay_s: int = 300,
    ) -> None:
        if retry_delay_s <= 0:
            raise ValueError("retry delay must be positive")
        self._jobs = jobs
        self._preparer = preparer
        self._committer = committer
        self._retry_delay_ns = retry_delay_s * 1_000_000_000

    def execute(self, lease: JobLease) -> ArtifactRef:
        if lease.job_type is not JobType.EPHEMERIS_LINK_BACKFILL:
            raise EphemerisLinkBackfillError("worker accepts link-backfill jobs only")
        try:
            return self._committer.commit(lease, self._preparer.prepare(lease))
        except Exception as error:
            try:
                self._jobs.fail(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    f"{type(error).__name__}: ephemeris link backfill failed",
                    UtcNs(time.time_ns() + self._retry_delay_ns),
                )
            except StaleLeaseError:
                pass
            raise


def ephemeris_link_payload(request: EphemerisLinkRequest) -> JobPayload:
    return JobPayload.create(
        EPHEMERIS_LINK_BACKFILL_JOB_SCHEMA,
        {
            "recording_id": str(request.recording_id),
            "source": request.source.value,
            "scope": request.scope,
            "policy": request.policy.value,
            "policy_ref": _artifact_document(request.policy_ref),
            "as_of_utc_ns": int(request.as_of_utc_ns),
        },
    )


def decode_ephemeris_link_payload(payload: JobPayload) -> EphemerisLinkRequest:
    if payload.schema != EPHEMERIS_LINK_BACKFILL_JOB_SCHEMA:
        raise EphemerisLinkBackfillError("unsupported link-backfill schema")
    item = cast(dict[str, object], thaw_value(payload.value))
    expected = {
        "recording_id",
        "source",
        "scope",
        "policy",
        "policy_ref",
        "as_of_utc_ns",
    }
    if set(item) != expected:
        raise EphemerisLinkBackfillError("payload fields differ from schema")
    as_of = item["as_of_utc_ns"]
    if isinstance(as_of, bool) or not isinstance(as_of, int):
        raise EphemerisLinkBackfillError("as_of_utc_ns must be an integer")
    try:
        return EphemerisLinkRequest(
            RecordingId(_string(item["recording_id"], "recording_id")),
            EphemerisSource(_string(item["source"], "source")),
            _string(item["scope"], "scope"),
            EphemerisSelectionPolicy(_string(item["policy"], "policy")),
            _artifact(item["policy_ref"]),
            UtcNs(as_of),
        )
    except EphemerisLinkBackfillError:
        raise
    except (TypeError, ValueError) as error:
        raise EphemerisLinkBackfillError(str(error)) from error


def _artifact(value: object) -> ArtifactRef:
    if not isinstance(value, dict) or set(value) != {"artifact_id", "digest", "schema"}:
        raise EphemerisLinkBackfillError("policy_ref fields differ from schema")
    digest = value["digest"]
    if not isinstance(digest, dict) or set(digest) != {"algorithm", "value"}:
        raise EphemerisLinkBackfillError("policy digest fields differ from schema")
    schema = value["schema"]
    schema_ref = None
    if schema is not None:
        if not isinstance(schema, dict) or set(schema) != {"schema_id", "version"}:
            raise EphemerisLinkBackfillError("policy schema fields differ")
        schema_ref = SchemaRef(
            _string(schema["schema_id"], "schema_id"),
            SchemaVersion.parse(_string(schema["version"], "version")),
        )
    return ArtifactRef(
        _string(value["artifact_id"], "artifact_id"),
        Digest(
            DigestAlgorithm(_string(digest["algorithm"], "digest.algorithm")),
            _string(digest["value"], "digest.value"),
        ),
        schema_ref,
    )


def _artifact_document(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {"algorithm": ref.digest.algorithm.value, "value": ref.digest.value},
        "schema": None
        if ref.schema is None
        else {"schema_id": ref.schema.schema_id, "version": str(ref.schema.version)},
    }


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise EphemerisLinkBackfillError(f"{name} must be a string")
    return value
