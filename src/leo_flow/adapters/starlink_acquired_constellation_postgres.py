"""PostgreSQL catalog and authoritative hardware resolver for acquired QAM v0.3."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
    CatalogedStarlinkAcquiredConstellationV0_3,
    StarlinkAcquiredConstellationConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationCatalogProjectionV0_3,
    StarlinkAcquiredConstellationProductRefV0_3,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkAcquiredConstellationCatalogV0_3:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_acquired_constellation(
        self,
        projection: StarlinkAcquiredConstellationCatalogProjectionV0_3,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAcquiredConstellationProductRefV0_3:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_acquired_constellation_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink_acquired_constellation(
        self, ref: StarlinkAcquiredConstellationProductRefV0_3
    ) -> CatalogedStarlinkAcquiredConstellationV0_3 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_exact_recording_starlink_acquired_constellation_v0_3(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            )
            row = cursor.fetchone()
        result = None if row is None else _cataloged(row)
        return result if result is not None and result.ref == ref else None

    def latest_starlink_acquired_constellation(
        self, recording_id: RecordingId
    ) -> StarlinkAcquiredConstellationProductRefV0_3 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_acquired_constellation_v0_3(%s)",
                (str(recording_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT * FROM public.read_exact_recording_starlink_acquired_constellation_v0_3(%s,%s,%s,%s)",
                (
                    row["analysis_id"],
                    str(recording_id),
                    row["bundle_digest_algorithm"],
                    row["bundle_digest_value"],
                ),
            )
            exact = cursor.fetchone()
        return None if exact is None else _cataloged(exact).ref


class PostgresRecordingReceiverLnbResolverV0_3:
    """Resolve only through the immutable hardware snapshot linked to a recording."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def lnb_id_for_recording_receiver(
        self, recording_id: RecordingId, receiver_chain_id: ReceiverChainId
    ) -> str:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT c.lnb_id FROM public.recording_hardware_link l JOIN public.hardware_receiver_chain c ON c.snapshot_id=l.hardware_snapshot_id WHERE l.recording_id=%s AND c.receiver_chain_id=%s",
                (str(recording_id), str(receiver_chain_id)),
            )
            rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0]["lnb_id"], str)
            or not rows[0]["lnb_id"]
        ):
            raise LookupError(
                "recording receiver has no unique authoritative LNB assignment"
            )
        return str(rows[0]["lnb_id"])


def publish_starlink_acquired_constellation_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkAcquiredConstellationCatalogProjectionV0_3,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkAcquiredConstellationProductRefV0_3:
    existing = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    if (
        not idempotency_key
        or existing is None
        or existing.recording_object != recording_ref
        or projection.recording_id != recording_ref.recording_id
        or projection.input_recording_digest != recording_ref.identity_digest()
    ):
        raise ValueError("acquired-QAM input is not the exact recording")
    cursor.execute(
        "SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)",
        (
            bundle_ref.digest.algorithm.value,
            bundle_ref.digest.value,
            bundle_ref.byte_count,
            bundle_ref.media_type,
            bundle_ref.format_id,
            bundle_ref.locator,
        ),
    )
    params = _parameters(projection, bundle_ref, idempotency_key)
    try:
        cursor.execute(
            "SELECT public.publish_recording_starlink_acquired_constellation_v0_3(%s) AS published",
            (Jsonb(params),),
        )
        row = cursor.fetchone()
    except psycopg.errors.UniqueViolation as error:
        raise StarlinkAcquiredConstellationConflictError(
            "acquired-QAM catalog identity was reused"
        ) from error
    if row is None or row["published"] is not True:
        raise StarlinkAcquiredConstellationConflictError(
            "acquired-QAM publication was not acknowledged"
        )
    return StarlinkAcquiredConstellationProductRefV0_3(
        projection.analysis_id, projection.recording_id, bundle_ref
    )


def _parameters(
    p: StarlinkAcquiredConstellationCatalogProjectionV0_3, ref: ObjectRef, key: str
) -> dict[str, object]:
    return {
        "analysis_id": p.analysis_id,
        "recording_id": str(p.recording_id),
        "input_recording_digest_algorithm": "sha256",
        "input_recording_digest_value": p.input_recording_digest.value,
        "source_suite_analysis_id": p.source_suite_ref.artifact_id,
        "source_suite_bundle_digest_algorithm": "sha256",
        "source_suite_bundle_digest_value": p.source_suite_ref.digest.value,
        "source_suite_request_digest_algorithm": "sha256",
        "source_suite_request_digest_value": p.source_suite_request_digest.value,
        "request_digest_algorithm": "sha256",
        "request_digest_value": p.request_digest.value,
        "bundle_digest_algorithm": "sha256",
        "bundle_digest_value": ref.digest.value,
        "stream_count": p.stream_count,
        "window_count": p.window_count,
        "point_count": p.point_count,
        "calibration_required": True,
        "idempotency_key": key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkAcquiredConstellationV0_3:
    source = ArtifactRef(
        str(row["source_suite_analysis_id"]),
        _digest(row, "source_suite_bundle"),
        SchemaRef(
            "org.leo-flow.starlink-detector-suite-recording-bundle", SchemaVersion(0, 2)
        ),
    )
    projection = StarlinkAcquiredConstellationCatalogProjectionV0_3(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        _digest(row, "input_recording"),
        source,
        _digest(row, "source_suite_request"),
        _digest(row, "request"),
        _int(row["stream_count"]),
        _int(row["window_count"]),
        _int(row["point_count"]),
        bool(row["calibration_required"]),
    )
    ref = ObjectRef(
        _digest(row, "bundle"),
        _int(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkAcquiredConstellationV0_3(projection, ref)


def _digest(row: dict[str, object], prefix: str) -> Digest:
    algorithm = row.get(f"{prefix}_digest_algorithm", "sha256")
    return Digest(DigestAlgorithm(str(algorithm)), str(row[f"{prefix}_digest_value"]))


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
