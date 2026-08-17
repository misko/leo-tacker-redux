"""Fenced atomic PostgreSQL commit and narrow reads for enhanced waterfall work."""

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
from leo_flow.analysis.recording.waterfall_v0_2_codec import (
    WATERFALL_V0_2_FORMAT_ID,
    WATERFALL_V0_2_MEDIA_TYPE,
    encode_waterfall_bundle_v0_2,
)
from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    WaterfallCatalogProjectionV0_2,
    waterfall_projection_v0_2,
)
from leo_flow.analysis.tracking.blind_doppler_codec import (
    BLIND_DOPPLER_FORMAT_ID,
    BLIND_DOPPLER_MEDIA_TYPE,
    encode_blind_doppler_bundle,
)
from leo_flow.analysis.tracking.doppler_persistence import (
    ADVANCED_DOPPLER_FORMAT_ID,
    ADVANCED_DOPPLER_MEDIA_TYPE,
    DopplerCatalogProjectionV0_1,
    doppler_projection_v0_1,
    encode_advanced_doppler_bundle,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    canonical_digest,
)
from leo_flow.contracts.doppler_evidence import (
    DopplerAnalysisId,
    DopplerAnalysisRefV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.waterfall import WaterfallProductId, WaterfallProductRefV0_1
from leo_flow.contracts.waterfall_v0_2 import WaterfallProductRefV0_2
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_sql import COMPLETE_SQL, LOCK_ACTIVE_SQL
from leo_flow.services.waterfall_analysis import PreparedWaterfallAnalysisV0_1
from leo_flow.services.waterfall_doppler_analysis import (
    PreparedCombinedWaterfallAnalysisV0_1,
)
from leo_flow.storage.ports import BlobWriter

from .waterfall_postgres_catalog import publish_waterfall_with_cursor

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class WaterfallDopplerConflictError(RuntimeError):
    pass


class AtomicPostgresWaterfallDopplerCommitterV0_1:
    """Upload immutable blobs, then catalog all products under one job fence."""

    def __init__(self, blobs: BlobWriter, connect: ConnectionFactory) -> None:
        self._blobs = blobs
        self._connect = connect

    def commit_waterfall(
        self, lease: JobLease, prepared: PreparedWaterfallAnalysisV0_1
    ) -> ArtifactRef:
        if lease.job_type is not JobType.WATERFALL_ANALYSIS:
            raise ValueError("committer accepts waterfall-analysis leases only")
        if not isinstance(prepared, PreparedCombinedWaterfallAnalysisV0_1):
            raise TypeError("enhanced committer requires combined preparation")

        legacy_projection = waterfall_projection_v0_1(prepared.request, prepared.bundle)
        enhanced = prepared.enhanced
        waterfall_projection = waterfall_projection_v0_2(
            enhanced.request, enhanced.waterfall
        )
        legacy_ref = self._put(
            encode_waterfall_bundle(prepared.bundle),
            WATERFALL_MEDIA_TYPE,
            WATERFALL_FORMAT_ID,
            f"waterfall-analysis:{lease.job_id}:bundle-v0.1",
        )
        waterfall_ref = self._put(
            encode_waterfall_bundle_v0_2(enhanced.waterfall),
            WATERFALL_V0_2_MEDIA_TYPE,
            WATERFALL_V0_2_FORMAT_ID,
            f"waterfall-analysis:{lease.job_id}:bundle-v0.2",
        )
        doppler_blobs = []
        for tile in enhanced.tiles:
            basic = self._put(
                encode_blind_doppler_bundle(tile.basic),
                BLIND_DOPPLER_MEDIA_TYPE,
                BLIND_DOPPLER_FORMAT_ID,
                f"waterfall-analysis:{lease.job_id}:{tile.spectrogram.input_identity_digest.value}:blind-v0.1",
            )
            advanced = self._put(
                encode_advanced_doppler_bundle(tile.advanced),
                ADVANCED_DOPPLER_MEDIA_TYPE,
                ADVANCED_DOPPLER_FORMAT_ID,
                f"waterfall-analysis:{lease.job_id}:{tile.spectrogram.input_identity_digest.value}:advanced-v0.1",
            )
            projection = doppler_projection_v0_1(
                enhanced.waterfall, waterfall_ref.digest, tile
            )
            doppler_blobs.append((projection, basic, advanced))

        result = ArtifactRef(
            str(prepared.bundle.product_id), legacy_ref.digest, prepared.bundle.schema
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(LOCK_ACTIVE_SQL, _lease(lease))
            if cursor.fetchone() is None:
                raise StaleLeaseError("waterfall lease is stale")
            legacy_product = publish_waterfall_with_cursor(
                cursor,
                legacy_projection,
                legacy_ref,
                prepared.request.recording_object_ref,
                idempotency_key=f"waterfall-analysis:{lease.job_id}",
            )
            _publish_legacy_projection_work(cursor, lease, legacy_product)
            _register_live_object(cursor, waterfall_ref)
            _publish_waterfall_v0_2(
                cursor,
                lease,
                waterfall_projection,
                waterfall_ref,
                str(legacy_product.product_id),
            )
            for projection, basic_ref, advanced_ref in doppler_blobs:
                _register_live_object(cursor, basic_ref)
                _register_live_object(cursor, advanced_ref)
                _publish_doppler(cursor, lease, projection, basic_ref, advanced_ref)
            cursor.execute(
                COMPLETE_SQL,
                {**_lease(lease), "result_ref": Jsonb(_artifact(result))},
            )
            if cursor.fetchone() is None:
                raise StaleLeaseError("waterfall lease became stale during completion")
        return result

    def _put(
        self, payload: bytes, media_type: str, format_id: str, idempotency_key: str
    ) -> ObjectRef:
        return self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=media_type,
            format_id=format_id,
            idempotency_key=idempotency_key,
        )


class PostgresWaterfallDopplerQueryV0_1:
    """Read only the database-owned public functions, never table layouts."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def list_recording_doppler(
        self, recording_id: RecordingId
    ) -> tuple[DopplerAnalysisRefV0_1, ...]:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_recording_doppler_analysis(%s)",
                (str(recording_id),),
            )
            rows = cursor.fetchall()
        return tuple(_doppler_ref(row) for row in rows)

    def get_waterfall_v0_2(self, product_id: str) -> WaterfallProductRefV0_2 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_recording_waterfall_v0_2(%s)",
                (product_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return WaterfallProductRefV0_2(
            WaterfallProductId(str(row["product_id"])),
            AnalysisRunId(str(row["analysis_run_id"])),
            RecordingId(str(row["recording_id"])),
            _object_ref(row, "bundle"),
        )


def _publish_legacy_projection_work(
    cursor: psycopg.Cursor[dict[str, object]],
    lease: JobLease,
    ref: WaterfallProductRefV0_1,
) -> None:
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
    row = cursor.fetchone()
    if row is None or row["inserted"] is not True:
        raise WaterfallDopplerConflictError("legacy projection work conflicts")


def _publish_waterfall_v0_2(
    cursor: psycopg.Cursor[dict[str, object]],
    lease: JobLease,
    projection: WaterfallCatalogProjectionV0_2,
    bundle_ref: ObjectRef,
    legacy_product_id: str,
) -> None:
    params = {
        "product_id": projection.product_id,
        "analysis_run_id": projection.analysis_run_id,
        "source_job_id": str(lease.job_id),
        "recording_id": projection.recording_id,
        "input_algorithm": projection.input_recording_digest.algorithm.value,
        "input_digest": projection.input_recording_digest.value,
        "request_algorithm": projection.request_digest.algorithm.value,
        "request_digest": projection.request_digest.value,
        "bundle_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest": bundle_ref.digest.value,
        "legacy_product_id": legacy_product_id,
        "tile_count": projection.tile_count,
        "pixel_count": projection.pixel_count,
        "key": f"waterfall-analysis:{lease.job_id}:v0.2",
    }
    cursor.execute(
        """SELECT public.publish_recording_waterfall_v0_2(
        %(product_id)s,%(analysis_run_id)s,%(source_job_id)s,%(recording_id)s,
        %(input_algorithm)s,%(input_digest)s,%(request_algorithm)s,%(request_digest)s,
        %(bundle_algorithm)s,%(bundle_digest)s,%(legacy_product_id)s,
        %(tile_count)s,%(pixel_count)s,%(key)s) AS inserted""",
        params,
    )
    row = cursor.fetchone()
    if row is not None and row["inserted"] is True:
        return
    cursor.execute(
        """SELECT * FROM public.recording_waterfall_v0_2
        WHERE product_id=%(product_id)s OR analysis_run_id=%(analysis_run_id)s
           OR source_job_id=%(source_job_id)s OR idempotency_key=%(key)s""",
        params,
    )
    rows = cursor.fetchall()
    expected = (
        params["product_id"],
        params["analysis_run_id"],
        params["source_job_id"],
        params["recording_id"],
        params["input_digest"],
        params["request_digest"],
        params["bundle_digest"],
        params["tile_count"],
        params["pixel_count"],
        params["key"],
    )
    if (
        len(rows) != 1
        or (
            rows[0]["product_id"],
            rows[0]["analysis_run_id"],
            rows[0]["source_job_id"],
            rows[0]["recording_id"],
            rows[0]["input_recording_digest_value"],
            rows[0]["request_digest_value"],
            rows[0]["bundle_digest_value"],
            rows[0]["tile_count"],
            rows[0]["pixel_count"],
            rows[0]["idempotency_key"],
        )
        != expected
    ):
        raise WaterfallDopplerConflictError("waterfall v0.2 identity conflicts")


def _publish_doppler(
    cursor: psycopg.Cursor[dict[str, object]],
    lease: JobLease,
    projection: DopplerCatalogProjectionV0_1,
    basic_ref: ObjectRef,
    advanced_ref: ObjectRef,
) -> None:
    params = {
        "doppler_id": str(projection.doppler_id),
        "source_job_id": str(lease.job_id),
        "recording_id": str(projection.recording_id),
        "waterfall_product_id": projection.waterfall_product_id,
        "waterfall_algorithm": projection.waterfall_bundle_digest.algorithm.value,
        "waterfall_digest": projection.waterfall_bundle_digest.value,
        "segment_id": str(projection.segment_id),
        "receiver_chain_id": str(projection.receiver_chain_id),
        "spectrogram_algorithm": projection.spectrogram_digest.algorithm.value,
        "spectrogram_digest": projection.spectrogram_digest.value,
        "basic_config_algorithm": projection.basic_config_digest.algorithm.value,
        "basic_config_digest": projection.basic_config_digest.value,
        "advanced_config_algorithm": projection.advanced_config_digest.algorithm.value,
        "advanced_config_digest": projection.advanced_config_digest.value,
        "basic_bundle_algorithm": basic_ref.digest.algorithm.value,
        "basic_bundle_digest": basic_ref.digest.value,
        "advanced_bundle_algorithm": advanced_ref.digest.algorithm.value,
        "advanced_bundle_digest": advanced_ref.digest.value,
        "candidate_count": projection.candidate_count,
        "moving_count": projection.moving_candidate_count,
        "strongest_score": projection.strongest_candidate_score,
        "key": f"waterfall-analysis:{lease.job_id}:{projection.doppler_id}",
    }
    cursor.execute(
        """SELECT public.publish_recording_doppler_analysis(
        %(doppler_id)s,%(source_job_id)s,%(recording_id)s,%(waterfall_product_id)s,
        %(waterfall_algorithm)s,%(waterfall_digest)s,%(segment_id)s,%(receiver_chain_id)s,
        %(spectrogram_algorithm)s,%(spectrogram_digest)s,
        %(basic_config_algorithm)s,%(basic_config_digest)s,
        %(advanced_config_algorithm)s,%(advanced_config_digest)s,
        %(basic_bundle_algorithm)s,%(basic_bundle_digest)s,
        %(advanced_bundle_algorithm)s,%(advanced_bundle_digest)s,
        %(candidate_count)s,%(moving_count)s,%(strongest_score)s,%(key)s) AS inserted""",
        params,
    )
    row = cursor.fetchone()
    if row is not None and row["inserted"] is True:
        return
    cursor.execute(
        """SELECT * FROM public.recording_doppler_analysis
        WHERE doppler_id=%(doppler_id)s OR idempotency_key=%(key)s OR
          (waterfall_product_id,segment_id,receiver_chain_id,spectrogram_digest_value,
           basic_config_digest_value,advanced_config_digest_value)=
          (%(waterfall_product_id)s,%(segment_id)s,%(receiver_chain_id)s,
           %(spectrogram_digest)s,%(basic_config_digest)s,%(advanced_config_digest)s)""",
        params,
    )
    rows = cursor.fetchall()
    if len(rows) != 1 or any(
        rows[0][name] != params[parameter]
        for name, parameter in (
            ("doppler_id", "doppler_id"),
            ("source_job_id", "source_job_id"),
            ("recording_id", "recording_id"),
            ("waterfall_product_id", "waterfall_product_id"),
            ("waterfall_bundle_digest_value", "waterfall_digest"),
            ("segment_id", "segment_id"),
            ("receiver_chain_id", "receiver_chain_id"),
            ("spectrogram_digest_value", "spectrogram_digest"),
            ("basic_config_digest_value", "basic_config_digest"),
            ("advanced_config_digest_value", "advanced_config_digest"),
            ("basic_bundle_digest_value", "basic_bundle_digest"),
            ("advanced_bundle_digest_value", "advanced_bundle_digest"),
            ("candidate_count", "candidate_count"),
            ("moving_candidate_count", "moving_count"),
            ("strongest_candidate_score", "strongest_score"),
            ("idempotency_key", "key"),
        )
    ):
        raise WaterfallDopplerConflictError("Doppler analysis identity conflicts")


def _register_live_object(
    cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef
) -> None:
    values = {
        "algorithm": ref.digest.algorithm.value,
        "digest": ref.digest.value,
        "bytes": ref.byte_count,
        "media": ref.media_type,
        "format": ref.format_id,
        "locator": ref.locator,
    }
    cursor.execute(
        "SELECT public.register_live_object_blob(%(algorithm)s,%(digest)s,%(bytes)s,%(media)s,%(format)s,%(locator)s)",
        values,
    )
    cursor.execute(
        """SELECT byte_count,media_type,format_id,locator FROM public.object_blob
        WHERE digest_algorithm=%(algorithm)s AND digest_value=%(digest)s
          AND lifecycle_state='live'""",
        values,
    )
    row = cursor.fetchone()
    if row is None or (
        row["byte_count"],
        row["media_type"],
        row["format_id"],
        row["locator"],
    ) != (ref.byte_count, ref.media_type, ref.format_id, ref.locator):
        raise WaterfallDopplerConflictError("analysis object metadata conflicts")


def _doppler_ref(row: dict[str, object]) -> DopplerAnalysisRefV0_1:
    return DopplerAnalysisRefV0_1(
        DopplerAnalysisId(str(row["doppler_id"])),
        RecordingId(str(row["recording_id"])),
        WaterfallProductId(str(row["waterfall_product_id"])),
        _digest(row, "waterfall"),
        SegmentId(str(row["segment_id"])),
        ReceiverChainId(str(row["receiver_chain_id"])),
        _digest(row, "spectrogram"),
        _digest(row, "basic_config"),
        _digest(row, "advanced_config"),
        _object_ref(row, "basic_bundle"),
        _object_ref(row, "advanced_bundle"),
        _integer(row["candidate_count"], "candidate_count"),
        _integer(row["moving_candidate_count"], "moving_candidate_count"),
        None
        if row["strongest_candidate_score"] is None
        else _number(row["strongest_candidate_score"], "strongest_candidate_score"),
    )


def _object_ref(row: dict[str, object], prefix: str) -> ObjectRef:
    return ObjectRef(
        _digest(row, prefix),
        _integer(row[f"{prefix}_byte_count"], f"{prefix}_byte_count"),
        str(row[f"{prefix}_media_type"]),
        str(row[f"{prefix}_format_id"]),
        str(row[f"{prefix}_locator"]),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WaterfallDopplerConflictError(f"database {name} is not an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WaterfallDopplerConflictError(f"database {name} is not numeric")
    return float(value)


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
