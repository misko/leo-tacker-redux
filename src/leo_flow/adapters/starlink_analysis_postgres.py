"""Atomic CAS/catalog publication and fenced Starlink job completion."""

from __future__ import annotations

import io
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_codec import (
    STARLINK_FORMAT_ID,
    STARLINK_MEDIA_TYPE,
    encode_starlink_bundle,
)
from leo_flow.analysis.recording.starlink_persistence import starlink_projection_v0_1
from leo_flow.contracts.core import ArtifactRef, Digest, canonical_digest
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.starlink_analysis import PreparedStarlinkAnalysisV0_1
from leo_flow.storage.ports import BlobWriter

from .starlink_postgres_catalog import publish_starlink_with_cursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class AtomicPostgresStarlinkCommitterV0_1:
    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit_starlink(
        self, lease: JobLease, prepared: PreparedStarlinkAnalysisV0_1
    ) -> ArtifactRef:
        if lease.job_type is not JobType.STARLINK_ANALYSIS:
            raise ValueError("committer accepts Starlink-analysis leases only")
        projection = starlink_projection_v0_1(prepared.request, prepared.bundle)
        payload = encode_starlink_bundle(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_MEDIA_TYPE,
            format_id=STARLINK_FORMAT_ID,
            idempotency_key=f"starlink-analysis:{lease.job_id}:bundle-v0.1",
        )
        result = ArtifactRef(
            prepared.bundle.analysis_id, bundle_ref.digest, prepared.bundle.schema
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, _lease(lease))
            if cursor.fetchone() is None:
                raise StaleLeaseError("Starlink lease is stale")
            ref = publish_starlink_with_cursor(
                cursor,
                projection,
                bundle_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"starlink-analysis:{lease.job_id}",
            )
            work_id = (
                "slwork_"
                + canonical_digest(
                    {
                        "source_job_id": str(lease.job_id),
                        "analysis_id": ref.analysis_id,
                        "bundle_digest": str(ref.bundle_ref.digest),
                    }
                ).value
            )
            cursor.execute(
                """SELECT public.publish_starlink_projection_work(
                %(work_id)s,%(job_id)s,%(lease_token)s,%(lease_generation)s,
                %(analysis_id)s,%(recording_id)s,%(algorithm)s,%(digest)s)
                AS inserted""",
                {
                    **_lease(lease),
                    "work_id": work_id,
                    "analysis_id": ref.analysis_id,
                    "recording_id": str(ref.recording_id),
                    "algorithm": ref.bundle_ref.digest.algorithm.value,
                    "digest": ref.bundle_ref.digest.value,
                },
            )
            row = cursor.fetchone()
            if row is None or row["inserted"] is not True:
                raise RuntimeError("Starlink projection work identity conflicts")
            cursor.execute(
                COMPLETE_SQL, {**_lease(lease), "result_ref": Jsonb(_artifact(result))}
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("Starlink lease became stale during completion")
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
