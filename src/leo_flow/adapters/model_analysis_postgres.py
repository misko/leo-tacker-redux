"""Atomic PostgreSQL ModelSnapshot publication and fenced job completion."""

from __future__ import annotations

import io
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.adapters.model_postgres_catalog import (
    publish_model_snapshot_with_cursor,
)
from leo_flow.analysis.model.codec import (
    MODEL_SNAPSHOT_FORMAT_ID,
    MODEL_SNAPSHOT_MEDIA_TYPE,
    encode_model_snapshot,
)
from leo_flow.analysis.model.persistence import model_snapshot_projection
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.model_analysis import PreparedModelAnalysis
from leo_flow.storage.ports import BlobWriter

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class AtomicPostgresModelAnalysisCommitter:
    """Upload first, then atomically publish a model and complete its lease."""

    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit(self, lease: JobLease, prepared: PreparedModelAnalysis) -> ArtifactRef:
        if lease.job_type is not JobType.MODEL_ANALYSIS:
            raise ValueError("committer accepts model-analysis leases only")
        projection = model_snapshot_projection(prepared.request, prepared.bundle)
        payload = encode_model_snapshot(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=MODEL_SNAPSHOT_MEDIA_TYPE,
            format_id=MODEL_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"model-analysis:{lease.job_id}:model-bundle",
        )
        result = ArtifactRef(
            str(prepared.bundle.model_snapshot_id),
            bundle_ref.digest,
            prepared.bundle.schema,
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, _lease_parameters(lease))
            if cursor.fetchone() is None:
                raise StaleLeaseError(
                    "lease token, generation, type, or expiry is stale"
                )
            publish_model_snapshot_with_cursor(
                cursor,
                projection,
                bundle_ref,
                prepared.request,
                prepared.bundle,
                idempotency_key=f"model-analysis:{lease.job_id}",
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
        "job_type": lease.job_type.value,
    }


def _artifact(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "schema_id": ref.schema.schema_id if ref.schema is not None else None,
        "schema_version": str(ref.schema.version) if ref.schema is not None else None,
    }
