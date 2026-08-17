"""Atomic PostgreSQL waterfall publication and fenced job completion."""

from __future__ import annotations

import io
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.waterfall_codec import (
    WATERFALL_FORMAT_ID,
    WATERFALL_MEDIA_TYPE,
    encode_waterfall_bundle,
)
from leo_flow.analysis.recording.waterfall_persistence import waterfall_projection_v0_1
from leo_flow.contracts.core import ArtifactRef, Digest, canonical_digest
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.waterfall_analysis import PreparedWaterfallAnalysisV0_1
from leo_flow.storage.ports import BlobWriter

from .waterfall_postgres_catalog import publish_waterfall_with_cursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class AtomicPostgresWaterfallCommitterV0_1:
    """Upload first; catalog, enqueue projection, and complete atomically."""

    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit_waterfall(
        self, lease: JobLease, prepared: PreparedWaterfallAnalysisV0_1
    ) -> ArtifactRef:
        if lease.job_type is not JobType.WATERFALL_ANALYSIS:
            raise ValueError("committer accepts waterfall-analysis leases only")
        projection = waterfall_projection_v0_1(prepared.request, prepared.bundle)
        payload = encode_waterfall_bundle(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=WATERFALL_MEDIA_TYPE,
            format_id=WATERFALL_FORMAT_ID,
            idempotency_key=f"waterfall-analysis:{lease.job_id}:bundle-v0.1",
        )
        result = ArtifactRef(
            str(prepared.bundle.product_id), bundle_ref.digest, prepared.bundle.schema
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, _lease(lease))
            if cursor.fetchone() is None:
                raise StaleLeaseError("waterfall lease is stale")
            ref = publish_waterfall_with_cursor(
                cursor,
                projection,
                bundle_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"waterfall-analysis:{lease.job_id}",
            )
            work_id = (
                "wfwork_"
                + canonical_digest(
                    {
                        "source_job_id": str(lease.job_id),
                        "product_id": str(ref.product_id),
                        "bundle_digest": str(ref.bundle_ref.digest),
                    }
                ).value
            )
            values = {
                "work_id": work_id,
                "source_job_id": str(lease.job_id),
                "product_id": str(ref.product_id),
                "analysis_run_id": str(ref.analysis_run_id),
                "recording_id": str(ref.recording_id),
                "algorithm": ref.bundle_ref.digest.algorithm.value,
                "digest": ref.bundle_ref.digest.value,
            }
            cursor.execute(
                """SELECT public.publish_waterfall_projection_work(
                %(work_id)s,%(source_job_id)s,%(product_id)s,%(analysis_run_id)s,
                %(recording_id)s,%(algorithm)s,%(digest)s) AS inserted""",
                values,
            )
            inserted = cursor.fetchone()
            if inserted is None or inserted["inserted"] is not True:
                raise RuntimeError("waterfall projection work identity conflicts")
            cursor.execute(
                COMPLETE_SQL,
                {**_lease(lease), "result_ref": Jsonb(_artifact(result))},
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("waterfall lease became stale during completion")
        return result


def _lease(lease: JobLease) -> dict[str, object]:
    return {
        "job_id": str(lease.job_id),
        "job_type": lease.job_type.value,
        "lease_token": lease.lease_token,
        "lease_generation": lease.lease_generation,
    }


def _artifact(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "schema_id": ref.schema.schema_id if ref.schema else None,
        "schema_version": str(ref.schema.version) if ref.schema else None,
    }
