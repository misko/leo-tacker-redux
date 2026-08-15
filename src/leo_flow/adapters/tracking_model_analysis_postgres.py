"""Atomic tracking-model publication and fenced job completion."""

from __future__ import annotations

import io
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.model.tracking_model_codec import (
    TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
    TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
    encode_tracking_model_snapshot,
)
from leo_flow.analysis.model.tracking_model_persistence import (
    TrackingModelIntegrityError,
    tracking_model_projection,
)
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.tracking_model_output import TrackingModelSnapshotBundle
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.model_analysis import (
    TRACKING_MODEL_ANALYSIS_JOB_SCHEMA,
    decode_tracking_model_analysis_payload,
)
from leo_flow.storage.ports import BlobWriter

from .tracking_model_postgres import publish_tracking_model_with_cursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class AtomicPostgresTrackingModelCommitter:
    """Upload first, then atomically publish output and complete its lease."""

    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit(
        self, lease: JobLease, bundle: TrackingModelSnapshotBundle
    ) -> ArtifactRef:
        if (
            lease.job_type is not JobType.MODEL_ANALYSIS
            or lease.payload.schema != TRACKING_MODEL_ANALYSIS_JOB_SCHEMA
        ):
            raise ValueError("committer accepts tracking-model leases only")
        request = decode_tracking_model_analysis_payload(lease.payload)
        evidence = bundle.evidence
        if (
            evidence.tracking_input_identity != request.tracking_input_identity
            or evidence.config_ref != request.model_config_ref
            or evidence.algorithm_ref != request.algorithm_ref
        ):
            raise TrackingModelIntegrityError(
                "tracking model output does not close over its leased request"
            )
        payload = encode_tracking_model_snapshot(bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
            format_id=TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"tracking-model:{lease.job_id}:bundle",
        )
        projection = tracking_model_projection(bundle, bundle_ref)
        result = ArtifactRef(
            str(bundle.model_snapshot_id), bundle_ref.digest, bundle.schema
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
            publish_tracking_model_with_cursor(
                cursor,
                projection,
                idempotency_key=f"tracking-model:{lease.job_id}",
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
