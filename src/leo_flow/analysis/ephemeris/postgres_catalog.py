"""Psycopg ephemeris catalog with atomic immutable-reference publication."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs.contracts import JobLease
from leo_flow.jobs.ports import StaleLeaseError

from . import postgres_sql
from .catalog import ArchivedEphemerisSnapshot, EphemerisCatalogConflictError
from .resolver import SnapshotRecord

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresEphemerisCatalogError(RuntimeError):
    pass


class EphemerisObjectCollisionError(PostgresEphemerisCatalogError):
    pass


class PostgresEphemerisSnapshotCatalog:
    """Persistent semantic peer of ``InMemoryEphemerisSnapshotCatalog``."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(self, archived: ArchivedEphemerisSnapshot) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            self._publish_with_cursor(cursor, archived)

    def get_by_retrieval(
        self, retrieval_id: EphemerisRetrievalId
    ) -> ArchivedEphemerisSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.GET_BY_RETRIEVAL_SQL,
                {"retrieval_id": str(retrieval_id)},
            )
            row = cursor.fetchone()
        return None if row is None else _archived_from_row(row)

    def get(self, snapshot_id: EphemerisSnapshotId) -> ArchivedEphemerisSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.GET_BY_SNAPSHOT_SQL,
                {"snapshot_id": str(snapshot_id)},
            )
            row = cursor.fetchone()
        return None if row is None else _archived_from_row(row)

    def history(
        self, source: EphemerisSource, scope: str
    ) -> tuple[SnapshotRecord, ...]:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                postgres_sql.HISTORY_SQL,
                {"source": source.value, "scope": scope},
            )
            rows = cursor.fetchall()
        return tuple(_snapshot_record_from_row(row) for row in rows)

    @staticmethod
    def _publish_with_cursor(
        cursor: psycopg.Cursor[dict[str, object]],
        archived: ArchivedEphemerisSnapshot,
    ) -> None:
        snapshot = archived.snapshot
        for ref in (
            snapshot.raw_object_ref,
            snapshot.normalized_object_ref,
            archived.provenance_object_ref,
        ):
            _register_object(cursor, ref)
        parameters = _snapshot_parameters(archived)
        cursor.execute(postgres_sql.PUBLISH_SNAPSHOT_SQL, parameters)
        if cursor.fetchone() is not None:
            return
        cursor.execute(postgres_sql.GET_CONFLICTS_SQL, parameters)
        rows = cursor.fetchall()
        if len(rows) != 1 or _archived_from_row(rows[0]) != archived:
            raise EphemerisCatalogConflictError(
                "snapshot or retrieval ID identifies different content"
            )


class PostgresFencedEphemerisCommitter:
    """Publish a snapshot and complete its active lease in one transaction."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_and_complete(
        self,
        lease: JobLease,
        archived: ArchivedEphemerisSnapshot,
        result_ref: ArtifactRef,
    ) -> None:
        _validate_commit_inputs(lease, archived, result_ref)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            lease_parameters = {
                "job_id": str(lease.job_id),
                "lease_token": lease.lease_token,
                "lease_generation": lease.lease_generation,
            }
            cursor.execute(postgres_sql.LOCK_ACTIVE_LEASE_SQL, lease_parameters)
            if cursor.fetchone() is None:
                raise StaleLeaseError("lease token, generation, or expiry is stale")
            PostgresEphemerisSnapshotCatalog._publish_with_cursor(cursor, archived)
            cursor.execute(
                """
                UPDATE job
                SET state = 'succeeded', result_ref = %(result_ref)s,
                    lease_token = NULL, lease_expires_utc = NULL
                WHERE job_id = %(job_id)s
                  AND state = 'leased'
                  AND lease_token = %(lease_token)s
                  AND lease_generation = %(lease_generation)s
                  AND lease_expires_utc > clock_timestamp()
                RETURNING job_id
                """,
                {**lease_parameters, "result_ref": Jsonb(_artifact_value(result_ref))},
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("lease changed while completing retrieval")


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _validate_commit_inputs(
    lease: JobLease,
    archived: ArchivedEphemerisSnapshot,
    result_ref: ArtifactRef,
) -> None:
    snapshot = archived.snapshot
    payload = dict(lease.payload.value)
    if (
        lease.job_type.value != "ephemeris_retrieval"
        or payload.get("retrieval_id") != str(snapshot.retrieval_id)
        or payload.get("source") != snapshot.source.value
        or payload.get("scope") != snapshot.scope
    ):
        raise ValueError("lease payload does not identify the ephemeris snapshot")
    expected_result = ArtifactRef(
        str(snapshot.snapshot_id),
        archived.provenance_object_ref.digest,
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
    )
    if result_ref != expected_result:
        raise ValueError("result reference does not identify snapshot provenance")


def _register_object(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
    parameters = _object_parameters(ref)
    cursor.execute(postgres_sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(postgres_sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or (
        _database_int(row["byte_count"], "byte_count") != ref.byte_count
        or row["media_type"] != ref.media_type
        or row["format_id"] != ref.format_id
        or row["locator"] != ref.locator
    ):
        raise EphemerisObjectCollisionError(
            f"object digest {ref.digest} identifies different metadata"
        )


def _snapshot_parameters(archived: ArchivedEphemerisSnapshot) -> dict[str, object]:
    snapshot = archived.snapshot
    parser_schema = snapshot.parser_ref.schema
    policy_schema = snapshot.validation.policy_ref.schema
    return {
        "snapshot_id": str(snapshot.snapshot_id),
        "retrieval_id": str(snapshot.retrieval_id),
        "source": snapshot.source.value,
        "scope": snapshot.scope,
        "retrieved_at_utc_ns": int(snapshot.retrieved_at_utc_ns),
        **_prefixed_object("raw", snapshot.raw_object_ref),
        **_prefixed_object("normalized", snapshot.normalized_object_ref),
        **_prefixed_object("provenance", archived.provenance_object_ref),
        **_prefixed_artifact("parser", snapshot.parser_ref),
        "parser_schema_id": None if parser_schema is None else parser_schema.schema_id,
        "parser_schema_version": None
        if parser_schema is None
        else str(parser_schema.version),
        "satellite_count": snapshot.satellite_count,
        "norad_id_set_digest_algorithm": snapshot.norad_id_set_digest.algorithm.value,
        "norad_id_set_digest_value": snapshot.norad_id_set_digest.value,
        "element_epoch_min_utc_ns": int(snapshot.element_epoch_min_utc_ns),
        "element_epoch_max_utc_ns": int(snapshot.element_epoch_max_utc_ns),
        **_prefixed_artifact("validation_policy", snapshot.validation.policy_ref),
        "validation_policy_schema_id": None
        if policy_schema is None
        else policy_schema.schema_id,
        "validation_policy_schema_version": None
        if policy_schema is None
        else str(policy_schema.version),
        "validation_reason_codes": Jsonb(list(snapshot.validation.reason_codes)),
        "attribution": snapshot.attribution,
        "request_spec_digest": archived.request_spec_digest,
    }


def _archived_from_row(row: dict[str, object]) -> ArchivedEphemerisSnapshot:
    parser_ref = _artifact_from_row(row, "parser")
    policy_ref = _artifact_from_row(row, "validation_policy")
    reason_codes = row["validation_reason_codes"]
    if not isinstance(reason_codes, list) or not all(
        isinstance(code, str) for code in reason_codes
    ):
        raise PostgresEphemerisCatalogError("validation reason codes are invalid")
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId(str(row["snapshot_id"])),
        EphemerisRetrievalId(str(row["retrieval_id"])),
        EphemerisSource(str(row["source"])),
        str(row["scope"]),
        UtcNs(_database_int(row["retrieved_at_utc_ns"], "retrieved_at_utc_ns")),
        _object_from_row(row, "raw"),
        _object_from_row(row, "normalized"),
        parser_ref,
        _database_int(row["satellite_count"], "satellite_count"),
        Digest(
            DigestAlgorithm(str(row["norad_id_set_digest_algorithm"])),
            str(row["norad_id_set_digest_value"]),
        ),
        UtcNs(
            _database_int(row["element_epoch_min_utc_ns"], "element_epoch_min_utc_ns")
        ),
        UtcNs(
            _database_int(row["element_epoch_max_utc_ns"], "element_epoch_max_utc_ns")
        ),
        ValidationResult(True, policy_ref, tuple(reason_codes)),
        str(row["attribution"]),
    )
    return ArchivedEphemerisSnapshot(
        snapshot,
        _object_from_row(row, "provenance"),
        str(row["request_spec_digest"]),
    )


def _snapshot_record_from_row(row: dict[str, object]) -> SnapshotRecord:
    source = EphemerisSource(str(row["source"]))
    snapshot_ref = EphemerisSnapshotRef(
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
    )
    return SnapshotRecord(
        snapshot_ref,
        UtcNs(_database_int(row["retrieved_at_utc_ns"], "retrieved_at_utc_ns")),
    )


def _object_parameters(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _prefixed_object(prefix: str, ref: ObjectRef) -> dict[str, object]:
    return {
        f"{prefix}_digest_algorithm": ref.digest.algorithm.value,
        f"{prefix}_digest_value": ref.digest.value,
    }


def _object_from_row(row: dict[str, object], prefix: str) -> ObjectRef:
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
            str(row[f"{prefix}_digest_value"]),
        ),
        _database_int(row[f"{prefix}_byte_count"], f"{prefix}_byte_count"),
        str(row[f"{prefix}_media_type"]),
        str(row[f"{prefix}_format_id"]),
        str(row[f"{prefix}_locator"]),
    )


def _prefixed_artifact(prefix: str, ref: ArtifactRef) -> dict[str, object]:
    return {
        f"{prefix}_artifact_id": ref.artifact_id,
        f"{prefix}_digest_algorithm": ref.digest.algorithm.value,
        f"{prefix}_digest_value": ref.digest.value,
    }


def _artifact_from_row(row: dict[str, object], prefix: str) -> ArtifactRef:
    schema_id = row[f"{prefix}_schema_id"]
    schema_version = row[f"{prefix}_schema_version"]
    schema = None
    if schema_id is not None and schema_version is not None:
        schema = SchemaRef(str(schema_id), SchemaVersion.parse(str(schema_version)))
    return ArtifactRef(
        str(row[f"{prefix}_artifact_id"]),
        Digest(
            DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
            str(row[f"{prefix}_digest_value"]),
        ),
        schema,
    )


def _artifact_value(ref: ArtifactRef) -> dict[str, object]:
    value: dict[str, object] = {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
    }
    if ref.schema is not None:
        value["schema_id"] = ref.schema.schema_id
        value["schema_version"] = str(ref.schema.version)
    return value


def _database_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresEphemerisCatalogError(f"database {field} is not an integer")
    return value
