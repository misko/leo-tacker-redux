"""Atomic PostgreSQL FeatureSet publication and fenced job completion."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import BinaryIO, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.codec import (
    FEATURE_SET_FORMAT_ID,
    FEATURE_SET_MEDIA_TYPE,
    encode_feature_set,
)
from leo_flow.analysis.recording.persistence import feature_set_projection
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL
from leo_flow.services.recording_analysis import PreparedRecordingAnalysis

from .feature_postgres_catalog import publish_feature_set_with_cursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class _BlobWriter(Protocol):
    def put(
        self,
        stream: BinaryIO,
        *,
        expected_digest: Digest,
        expected_bytes: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef: ...


class AtomicPostgresRecordingAnalysisCommitter:
    """Upload first, then atomically publish FeatureSet and complete its lease."""

    def __init__(self, blobs: _BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit(
        self, lease: JobLease, prepared: PreparedRecordingAnalysis
    ) -> ArtifactRef:
        if lease.job_type is not JobType.RECORDING_ANALYSIS:
            raise ValueError("committer accepts recording-analysis leases only")
        projection = feature_set_projection(prepared.request, prepared.bundle)
        payload = encode_feature_set(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=FEATURE_SET_MEDIA_TYPE,
            format_id=FEATURE_SET_FORMAT_ID,
            idempotency_key=f"recording-analysis:{lease.job_id}:feature-bundle",
        )
        result = ArtifactRef(
            str(prepared.bundle.feature_set_id),
            bundle_ref.digest,
            prepared.bundle.schema,
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """
                SELECT job_id FROM job
                WHERE job_id = %(job_id)s
                  AND job_type = 'recording_analysis'
                  AND state = 'leased'
                  AND lease_token = %(lease_token)s
                  AND lease_generation = %(lease_generation)s
                  AND lease_expires_utc > clock_timestamp()
                FOR UPDATE
                """,
                _lease_parameters(lease),
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError(
                    "lease token, generation, type, or expiry is stale"
                )
            publish_feature_set_with_cursor(
                cursor,
                projection,
                bundle_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"recording-analysis:{lease.job_id}",
            )
            cursor.execute(
                COMPLETE_SQL,
                {**_lease_parameters(lease), "result_ref": Jsonb(_artifact(result))},
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("lease became stale during atomic completion")
        return result


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _lease_parameters(lease: JobLease) -> dict[str, object]:
    return {
        "job_id": str(lease.job_id),
        "lease_token": lease.lease_token,
        "lease_generation": lease.lease_generation,
    }


def _artifact(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "schema_id": ref.schema.schema_id if ref.schema is not None else None,
        "schema_version": str(ref.schema.version) if ref.schema is not None else None,
    }
