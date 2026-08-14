"""Fenced authoritative recording-to-ephemeris link publication."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.ephemeris.backfill import PreparedEphemerisLink
from leo_flow.analysis.ephemeris.resolver import (
    SnapshotRecord,
    TemporalEphemerisResolver,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    EphemerisSnapshotId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef, EphemerisSource
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
LINK_SCHEMA = SchemaRef("org.leo-flow.recording-ephemeris-link")


class EphemerisLinkPersistenceError(RuntimeError):
    pass


class RecordingAuthorityMismatchError(EphemerisLinkPersistenceError):
    pass


class EphemerisLinkConflictError(EphemerisLinkPersistenceError):
    pass


class AtomicPostgresEphemerisLinkCommitter:
    """Serialize selection, publish the link, and complete one lease."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def commit(self, lease: JobLease, prepared: PreparedEphemerisLink) -> ArtifactRef:
        if lease.job_type is not JobType.EPHEMERIS_LINK_BACKFILL:
            raise ValueError("committer accepts ephemeris-link-backfill leases only")
        request = prepared.request
        lease_parameters = {
            "job_id": str(lease.job_id),
            "lease_token": lease.lease_token,
            "lease_generation": lease.lease_generation,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """
                SELECT job_id FROM job
                WHERE job_id = %(job_id)s
                  AND job_type = 'ephemeris_link_backfill'
                  AND state = 'leased'
                  AND lease_token = %(lease_token)s
                  AND lease_generation = %(lease_generation)s
                  AND lease_expires_utc > clock_timestamp()
                FOR UPDATE
                """,
                lease_parameters,
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("link-backfill lease is stale")
            _verify_recording(cursor, prepared)
            # The ephemeris insert trigger takes the same transaction-scoped
            # key, stabilizing this provider/scope history without UPDATE rights.
            cursor.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(length(%s)::text || ':' || %s || %s, 0))",
                (
                    request.source.value,
                    request.source.value,
                    request.scope,
                ),
            )
            cursor.execute(
                """
                SELECT snapshot_id, source, retrieved_at_utc_ns,
                       raw_digest_algorithm, raw_digest_value,
                       normalized_digest_algorithm, normalized_digest_value
                FROM ephemeris_snapshot
                WHERE source = %(source)s AND scope = %(scope)s
                  AND retrieved_at_utc_ns <= %(as_of_utc_ns)s
                ORDER BY retrieved_at_utc_ns, snapshot_id
                """,
                {
                    "source": request.source.value,
                    "scope": request.scope,
                    "as_of_utc_ns": int(request.as_of_utc_ns),
                },
            )
            history = tuple(_snapshot_record(row) for row in cursor.fetchall())
            selection = TemporalEphemerisResolver(history, request.policy).resolve(
                request.source,
                prepared.recording_interval,
                request.policy_ref,
                request.as_of_utc_ns,
            )
            identity = {
                "recording_identity_digest": str(
                    prepared.recording_ref.identity_digest()
                ),
                "recording_interval": prepared.recording_interval,
                "source": request.source.value,
                "scope": request.scope,
                "policy": request.policy.value,
                "policy_ref": request.policy_ref,
                "as_of_utc_ns": request.as_of_utc_ns,
                "snapshot_ref": selection.snapshot_ref,
            }
            link_digest = canonical_digest(identity)
            link_id = f"ephlink_{link_digest.value[:32]}"
            parameters = {
                "link_id": link_id,
                "recording_id": str(request.recording_id),
                "recording_identity_digest_algorithm": "sha256",
                "recording_identity_digest_value": prepared.recording_ref.identity_digest().value,
                "recording_started_utc_ns": int(
                    prepared.recording_interval.started_utc_ns
                ),
                "recording_finished_utc_ns": int(
                    prepared.recording_interval.finished_utc_ns
                ),
                "source": request.source.value,
                "scope": request.scope,
                "selection_policy": request.policy.value,
                "policy_artifact_id": request.policy_ref.artifact_id,
                "policy_digest_algorithm": request.policy_ref.digest.algorithm.value,
                "policy_digest_value": request.policy_ref.digest.value,
                "policy_schema_id": None
                if request.policy_ref.schema is None
                else request.policy_ref.schema.schema_id,
                "policy_schema_version": None
                if request.policy_ref.schema is None
                else str(request.policy_ref.schema.version),
                "as_of_utc_ns": int(request.as_of_utc_ns),
                "snapshot_id": str(selection.snapshot_ref.snapshot_id),
                "raw_digest_algorithm": selection.snapshot_ref.raw_digest.algorithm.value,
                "raw_digest_value": selection.snapshot_ref.raw_digest.value,
                "normalized_digest_algorithm": selection.snapshot_ref.normalized_digest.algorithm.value,
                "normalized_digest_value": selection.snapshot_ref.normalized_digest.value,
                "link_digest_algorithm": link_digest.algorithm.value,
                "link_digest_value": link_digest.value,
                "idempotency_key": f"ephemeris-link:{lease.job_id}",
            }
            cursor.execute(_PUBLISH_SQL, parameters)
            if cursor.fetchone() is None:
                cursor.execute(_CONFLICT_SQL, parameters)
                row = cursor.fetchone()
                if row is None or any(
                    row[key] != parameters[key] for key in parameters
                ):
                    raise EphemerisLinkConflictError(
                        "link identity or idempotency key identifies different content"
                    )
            result = ArtifactRef(link_id, link_digest, LINK_SCHEMA)
            cursor.execute(
                COMPLETE_SQL,
                {**lease_parameters, "result_ref": Jsonb(_artifact(result))},
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("lease changed while publishing link")
        return result


def _verify_recording(
    cursor: psycopg.Cursor[dict[str, object]], prepared: PreparedEphemerisLink
) -> None:
    ref = prepared.recording_ref
    cursor.execute(
        """
        SELECT data_digest_algorithm, data_digest_value,
               metadata_digest_algorithm, metadata_digest_value,
               manifest_digest_value
        FROM recording WHERE recording_id = %s AND state = 'published'
        """,
        (str(ref.recording_id),),
    )
    row = cursor.fetchone()
    expected = (
        ref.data_object.digest.algorithm.value,
        ref.data_object.digest.value,
        ref.metadata_object.digest.algorithm.value,
        ref.metadata_object.digest.value,
        ref.manifest_digest.value,
    )
    if row is None or tuple(row.values()) != expected:
        raise RecordingAuthorityMismatchError(
            "recording reference is not authoritative"
        )


def _snapshot_record(row: dict[str, object]) -> SnapshotRecord:
    source = EphemerisSource(str(row["source"]))
    return SnapshotRecord(
        EphemerisSnapshotRef(
            EphemerisSnapshotId(str(row["snapshot_id"])),
            source,
            Digest(
                DigestAlgorithm(str(row["raw_digest_algorithm"])),
                str(row["raw_digest_value"]),
            ),
            Digest(
                DigestAlgorithm(str(row["normalized_digest_algorithm"])),
                str(row["normalized_digest_value"]),
            ),
        ),
        UtcNs(_database_int(row["retrieved_at_utc_ns"], "retrieved_at_utc_ns")),
    )


def _database_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EphemerisLinkPersistenceError(f"database {name} is not an integer")
    return value


def _artifact(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "schema_id": ref.schema.schema_id if ref.schema else None,
        "schema_version": str(ref.schema.version) if ref.schema else None,
    }


_COLUMNS = """link_id, recording_id, recording_identity_digest_algorithm,
recording_identity_digest_value, recording_started_utc_ns,
recording_finished_utc_ns, source, scope, selection_policy, policy_artifact_id,
policy_digest_algorithm, policy_digest_value, policy_schema_id,
policy_schema_version, as_of_utc_ns, snapshot_id, raw_digest_algorithm,
raw_digest_value, normalized_digest_algorithm, normalized_digest_value,
link_digest_algorithm, link_digest_value, idempotency_key"""
_VALUES = ", ".join(
    f"%({name.strip()})s" for name in _COLUMNS.replace("\n", " ").split(",")
)
_PUBLISH_SQL = f"INSERT INTO recording_ephemeris_link ({_COLUMNS}) VALUES ({_VALUES}) ON CONFLICT DO NOTHING RETURNING link_id"
_CONFLICT_SQL = f"SELECT {_COLUMNS} FROM recording_ephemeris_link WHERE link_id = %(link_id)s OR idempotency_key = %(idempotency_key)s"
