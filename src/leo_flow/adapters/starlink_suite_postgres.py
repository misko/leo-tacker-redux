"""Atomic catalog/outbox adapters for v0.2 Starlink detector-suite products."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
    starlink_pilot_constellation_projection_v0_1,
)
from leo_flow.analysis.recording.starlink_pilot_constellation_recording_codec import (
    STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID,
    STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE,
    encode_starlink_pilot_constellation_recording,
)
from leo_flow.analysis.recording.starlink_suite_codec import (
    STARLINK_SUITE_FORMAT_ID,
    STARLINK_SUITE_MEDIA_TYPE,
    encode_starlink_suite_bundle,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    CatalogedStarlinkSuiteV0_2,
    StarlinkSuiteCatalogProjectionV0_2,
    starlink_suite_projection_v0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    starlink_surrogate_null_projection_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_recording_codec import (
    STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID,
    STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE,
    encode_starlink_surrogate_null_recording,
)
from leo_flow.application.starlink_suite_projection_work import (
    StarlinkSuiteProjectionLeaseV0_2,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    JobId,
    RecordingId,
    canonical_digest,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.starlink_suite_analysis import PreparedStarlinkSuiteAnalysisV0_2
from leo_flow.services.starlink_suite_surrogate_analysis import (
    PreparedCombinedStarlinkSuiteAnalysisV0_2,
)
from leo_flow.storage.ports import BlobWriter
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
_SELECT = """SELECT s.*,o.byte_count AS bundle_byte_count,o.media_type AS bundle_media_type,o.format_id AS bundle_format_id,o.locator AS bundle_locator FROM public.recording_starlink_detector_suite s JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value) WHERE o.lifecycle_state='live'"""


class PostgresStarlinkSuiteCatalogV0_2:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_suite(
        self,
        projection: StarlinkSuiteCatalogProjectionV0_2,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkDetectorSuiteProductRefV0_2:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_suite_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink_suite(
        self, ref: StarlinkDetectorSuiteProductRefV0_2
    ) -> CatalogedStarlinkSuiteV0_2 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                _SELECT
                + " AND s.analysis_id=%s AND s.recording_id=%s AND s.bundle_digest_algorithm=%s AND s.bundle_digest_value=%s",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        result = _cataloged(row)
        return result if result.bundle_ref == ref.bundle_ref else None


def publish_starlink_suite_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkSuiteCatalogProjectionV0_2,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkDetectorSuiteProductRefV0_2:
    existing = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    if (
        not idempotency_key
        or existing is None
        or existing.recording_object != recording_ref
        or projection.recording_id != str(recording_ref.recording_id)
        or projection.input_recording_digest != recording_ref.identity_digest()
    ):
        raise ValueError("detector-suite input is not the exact recording")
    values = {
        "algorithm": bundle_ref.digest.algorithm.value,
        "digest": bundle_ref.digest.value,
        "bytes": bundle_ref.byte_count,
        "media": bundle_ref.media_type,
        "format": bundle_ref.format_id,
        "locator": bundle_ref.locator,
    }
    cursor.execute(
        "SELECT public.register_live_object_blob(%(algorithm)s,%(digest)s,%(bytes)s,%(media)s,%(format)s,%(locator)s)",
        values,
    )
    params = {
        "analysis_id": projection.analysis_id,
        "recording_id": projection.recording_id,
        "input_algorithm": projection.input_recording_digest.algorithm.value,
        "input_digest": projection.input_recording_digest.value,
        "request_algorithm": projection.request_digest.algorithm.value,
        "request_digest": projection.request_digest.value,
        "bundle_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest": bundle_ref.digest.value,
        "state": projection.state,
        "suite_count": projection.suite_count,
        "method_count": projection.method_count,
        "key": idempotency_key,
    }
    cursor.execute(
        "SELECT public.publish_recording_starlink_detector_suite(%(analysis_id)s,%(recording_id)s,%(input_algorithm)s,%(input_digest)s,%(request_algorithm)s,%(request_digest)s,%(bundle_algorithm)s,%(bundle_digest)s,%(state)s,%(suite_count)s,%(method_count)s,%(key)s) AS inserted",
        params,
    )
    row = cursor.fetchone()
    if row is not None and row["inserted"] is True:
        return StarlinkDetectorSuiteProductRefV0_2(
            projection.analysis_id, RecordingId(projection.recording_id), bundle_ref
        )
    cursor.execute(
        _SELECT
        + " AND (s.analysis_id=%(analysis_id)s OR s.idempotency_key=%(key)s OR (s.recording_id,s.request_digest_algorithm,s.request_digest_value)=(%(recording_id)s,%(request_algorithm)s,%(request_digest)s))",
        params,
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError("detector-suite catalog identity conflict")
    found = _cataloged(rows[0])
    if (
        found.projection != projection
        or found.bundle_ref != bundle_ref
        or rows[0]["idempotency_key"] != idempotency_key
    ):
        raise RuntimeError("detector-suite catalog identity reused")
    return found.ref


class AtomicPostgresStarlinkSuiteCommitterV0_2:
    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs, self._connect = blobs, connect

    def commit_starlink_suite(
        self, lease: JobLease, prepared: PreparedStarlinkSuiteAnalysisV0_2
    ) -> ArtifactRef:
        if lease.job_type is not JobType.STARLINK_SUITE_ANALYSIS:
            raise ValueError("committer accepts detector-suite leases only")
        projection = starlink_suite_projection_v0_2(prepared.request, prepared.bundle)
        payload = encode_starlink_suite_bundle(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=STARLINK_SUITE_MEDIA_TYPE,
            format_id=STARLINK_SUITE_FORMAT_ID,
            idempotency_key=f"starlink-suite:{lease.job_id}:bundle-v0.2",
        )
        result = ArtifactRef(
            prepared.bundle.analysis_id, bundle_ref.digest, prepared.bundle.schema
        )
        lp = {
            "job_id": str(lease.job_id),
            "job_type": lease.job_type.value,
            "lease_token": lease.lease_token,
            "lease_generation": lease.lease_generation,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, lp)
            if cursor.fetchone() is None:
                raise StaleLeaseError("detector-suite lease is stale")
            ref = publish_starlink_suite_with_cursor(
                cursor,
                projection,
                bundle_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"starlink-suite:{lease.job_id}",
            )
            work_id = (
                "slsuitework_"
                + canonical_digest(
                    {
                        "source_job_id": str(lease.job_id),
                        "analysis_id": ref.analysis_id,
                        "bundle_digest": str(ref.bundle_ref.digest),
                    }
                ).value
            )
            cursor.execute(
                "SELECT public.publish_starlink_detector_suite_projection_work(%(work_id)s,%(job_id)s,%(lease_token)s,%(lease_generation)s,%(analysis_id)s,%(recording_id)s,%(algorithm)s,%(digest)s) AS inserted",
                {
                    **lp,
                    "work_id": work_id,
                    "analysis_id": ref.analysis_id,
                    "recording_id": str(ref.recording_id),
                    "algorithm": ref.bundle_ref.digest.algorithm.value,
                    "digest": ref.bundle_ref.digest.value,
                },
            )
            row = cursor.fetchone()
            if row is None or row["inserted"] is not True:
                raise RuntimeError("detector-suite projection identity conflict")
            cursor.execute(
                COMPLETE_SQL,
                {
                    **lp,
                    "result_ref": Jsonb(
                        {
                            "artifact_id": result.artifact_id,
                            "digest_algorithm": result.digest.algorithm.value,
                            "digest_value": result.digest.value,
                            "schema_id": result.schema.schema_id
                            if result.schema
                            else None,
                            "schema_version": str(result.schema.version)
                            if result.schema
                            else None,
                        }
                    ),
                },
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("detector-suite lease became stale")
        return result


class AtomicPostgresCombinedStarlinkSuiteCommitterV0_2:
    """Publish suite, surrogate and optional QAM before one fenced completion."""

    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs, self._connect = blobs, connect

    def commit_starlink_suite(
        self, lease: JobLease, prepared: PreparedStarlinkSuiteAnalysisV0_2
    ) -> ArtifactRef:
        from leo_flow.adapters.starlink_pilot_constellation_postgres import (
            publish_starlink_pilot_constellation_with_cursor,
        )
        from leo_flow.adapters.starlink_surrogate_null_postgres import (
            publish_starlink_surrogate_null_with_cursor,
        )

        if lease.job_type is not JobType.STARLINK_SUITE_ANALYSIS or not isinstance(
            prepared, PreparedCombinedStarlinkSuiteAnalysisV0_2
        ):
            raise ValueError("combined committer accepts prepared suite jobs only")
        suite_projection = starlink_suite_projection_v0_2(
            prepared.request, prepared.bundle
        )
        suite_payload = encode_starlink_suite_bundle(prepared.bundle)
        suite_ref = self._put(
            suite_payload,
            STARLINK_SUITE_MEDIA_TYPE,
            STARLINK_SUITE_FORMAT_ID,
            f"starlink-suite:{lease.job_id}:bundle-v0.2",
        )
        surrogate_projection = starlink_surrogate_null_projection_v0_1(
            prepared.surrogate_null.request, prepared.surrogate_null.bundle
        )
        surrogate_payload = encode_starlink_surrogate_null_recording(
            prepared.surrogate_null.bundle
        )
        surrogate_ref = self._put(
            surrogate_payload,
            STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE,
            STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID,
            f"starlink-suite:{lease.job_id}:surrogate-null-bundle-v0.1",
        )
        constellation_projection = None
        constellation_ref = None
        if prepared.pilot_constellation is not None:
            constellation_projection = starlink_pilot_constellation_projection_v0_1(
                prepared.pilot_constellation.request,
                prepared.pilot_constellation.bundle,
            )
            constellation_payload = encode_starlink_pilot_constellation_recording(
                prepared.pilot_constellation.bundle
            )
            constellation_ref = self._put(
                constellation_payload,
                STARLINK_PILOT_CONSTELLATION_RECORDING_MEDIA_TYPE,
                STARLINK_PILOT_CONSTELLATION_RECORDING_FORMAT_ID,
                f"starlink-suite:{lease.job_id}:pilot-constellation-bundle-v0.1",
            )
        result = ArtifactRef(
            prepared.bundle.analysis_id, suite_ref.digest, prepared.bundle.schema
        )
        lp = {
            "job_id": str(lease.job_id),
            "job_type": lease.job_type.value,
            "lease_token": lease.lease_token,
            "lease_generation": lease.lease_generation,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, lp)
            if cursor.fetchone() is None:
                raise StaleLeaseError("detector-suite lease is stale")
            published_suite = publish_starlink_suite_with_cursor(
                cursor,
                suite_projection,
                suite_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"starlink-suite:{lease.job_id}",
            )
            publish_starlink_surrogate_null_with_cursor(
                cursor,
                surrogate_projection,
                surrogate_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"starlink-suite:{lease.job_id}:surrogate-null",
            )
            if constellation_projection is not None and constellation_ref is not None:
                publish_starlink_pilot_constellation_with_cursor(
                    cursor,
                    constellation_projection,
                    constellation_ref,
                    prepared.request.recording_object_ref,
                    idempotency_key=(
                        f"starlink-suite:{lease.job_id}:pilot-constellation"
                    ),
                )
            work_id = (
                "slsuitework_"
                + canonical_digest(
                    {
                        "source_job_id": str(lease.job_id),
                        "analysis_id": published_suite.analysis_id,
                        "bundle_digest": str(published_suite.bundle_ref.digest),
                    }
                ).value
            )
            cursor.execute(
                "SELECT public.publish_starlink_detector_suite_projection_work(%(work_id)s,%(job_id)s,%(lease_token)s,%(lease_generation)s,%(analysis_id)s,%(recording_id)s,%(algorithm)s,%(digest)s) AS inserted",
                {
                    **lp,
                    "work_id": work_id,
                    "analysis_id": published_suite.analysis_id,
                    "recording_id": str(published_suite.recording_id),
                    "algorithm": published_suite.bundle_ref.digest.algorithm.value,
                    "digest": published_suite.bundle_ref.digest.value,
                },
            )
            row = cursor.fetchone()
            if row is None or row["inserted"] is not True:
                raise RuntimeError("detector-suite projection identity conflict")
            cursor.execute(
                COMPLETE_SQL,
                {
                    **lp,
                    "result_ref": Jsonb(
                        {
                            "artifact_id": result.artifact_id,
                            "digest_algorithm": result.digest.algorithm.value,
                            "digest_value": result.digest.value,
                            "schema_id": result.schema.schema_id
                            if result.schema
                            else None,
                            "schema_version": str(result.schema.version)
                            if result.schema
                            else None,
                        }
                    ),
                },
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("detector-suite lease became stale")
        return result

    def _put(
        self, payload: bytes, media_type: str, format_id: str, key: str
    ) -> ObjectRef:
        return self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=media_type,
            format_id=format_id,
            idempotency_key=key,
        )


class PostgresStarlinkSuiteProjectionWorkRepositoryV0_2:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        source_job_ids: tuple[JobId, ...] | None = None,
    ) -> None:
        self._connect = connect
        if source_job_ids is not None and (
            not 1 <= len(source_job_ids) <= 72
            or len(set(source_job_ids)) != len(source_job_ids)
        ):
            raise ValueError(
                "detector-suite projection scope requires 1..72 unique jobs"
            )
        self._source_job_ids = source_job_ids

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkSuiteProjectionLeaseV0_2 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("projection bounds must be positive")
        token = f"{worker_id}:{uuid.uuid4().hex}"
        with self._connect() as connection:
            if self._source_job_ids is None:
                rows = connection.execute(
                    "SELECT * FROM public.claim_starlink_detector_suite_projection_work(%s,%s)",
                    (token, timedelta(seconds=lease_ttl_s)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM public.claim_campaign_starlink_suite_projection(%s,%s,%s)",
                    (
                        [str(value) for value in self._source_job_ids],
                        token,
                        timedelta(seconds=lease_ttl_s),
                    ),
                ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise RuntimeError("detector-suite projection claim is ambiguous")
            row = rows[0]
            receipts = connection.execute(
                "SELECT * FROM public.read_starlink_detector_suite_receipt(%s)",
                (row["source_job_id"],),
            ).fetchall()
        if len(receipts) != 1:
            raise RuntimeError("detector-suite receipt is missing")
        receipt = receipts[0]
        ref = StarlinkDetectorSuiteProductRefV0_2(
            str(receipt["analysis_id"]),
            RecordingId(str(receipt["recording_id"])),
            ObjectRef(
                Digest(
                    DigestAlgorithm(str(receipt["bundle_digest_algorithm"])),
                    str(receipt["bundle_digest_value"]),
                ),
                _int(receipt["bundle_byte_count"]),
                str(receipt["bundle_media_type"]),
                str(receipt["bundle_format_id"]),
                str(receipt["bundle_locator"]),
            ),
        )
        return StarlinkSuiteProjectionLeaseV0_2(
            str(row["work_id"]),
            JobId(str(row["source_job_id"])),
            ref,
            token,
            _int(row["lease_generation"]),
            _int(row["attempt"]),
        )

    def complete(self, lease: StarlinkSuiteProjectionLeaseV0_2) -> None:
        self._transition("complete_starlink_detector_suite_projection_work", lease)

    def park(self, lease: StarlinkSuiteProjectionLeaseV0_2, reason: str) -> None:
        self._transition("park_starlink_detector_suite_projection_work", lease, reason)

    def retry(
        self, lease: StarlinkSuiteProjectionLeaseV0_2, reason: str, delay_s: float
    ) -> None:
        self._transition(
            "retry_starlink_detector_suite_projection_work",
            lease,
            reason,
            timedelta(seconds=delay_s),
        )

    def _transition(
        self, function: str, lease: StarlinkSuiteProjectionLeaseV0_2, *extra: object
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT public.{function}(%s,%s,%s{',%s' * len(extra)}) AS changed",
                (lease.work_id, lease.lease_token, lease.lease_generation, *extra),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise RuntimeError("detector-suite projection lease is stale")


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkSuiteV0_2:
    projection = StarlinkSuiteCatalogProjectionV0_2(
        str(row["analysis_id"]),
        str(row["recording_id"]),
        _digest(row, "input_recording"),
        _digest(row, "request"),
        str(row["result_state"]),
        _int(row["suite_count"]),
        _int(row["method_count"]),
    )
    return CatalogedStarlinkSuiteV0_2(
        projection,
        ObjectRef(
            _digest(row, "bundle"),
            _int(row["bundle_byte_count"]),
            str(row["bundle_media_type"]),
            str(row["bundle_format_id"]),
            str(row["bundle_locator"]),
        ),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
