"""PostgreSQL catalog adapter for temporal pilot products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_temporal_pilot_persistence import (
    CatalogedStarlinkTemporalPilotV0_1,
    StarlinkTemporalPilotConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    StarlinkTemporalPilotCatalogProjectionV0_1,
    StarlinkTemporalPilotProductRefV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkTemporalPilotCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_temporal_pilot(
        self,
        projection: StarlinkTemporalPilotCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkTemporalPilotProductRefV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_temporal_pilot_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink_temporal_pilot(
        self, ref: StarlinkTemporalPilotProductRefV0_1
    ) -> CatalogedStarlinkTemporalPilotV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_recording_starlink_temporal_pilot(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else _cataloged(row)

    def latest_starlink_temporal_pilot(
        self, recording_id: RecordingId
    ) -> StarlinkTemporalPilotProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_temporal_pilot(%s)",
                (str(recording_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT * FROM public.read_recording_starlink_temporal_pilot(%s,%s,%s,%s)",
                (
                    row["analysis_id"],
                    str(recording_id),
                    row["bundle_digest_algorithm"],
                    row["bundle_digest_value"],
                ),
            )
            full = cursor.fetchone()
        return None if full is None else _cataloged(full).ref


def publish_starlink_temporal_pilot_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkTemporalPilotCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkTemporalPilotProductRefV0_1:
    existing = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    source = projection.source_suite_ref
    if (
        not idempotency_key
        or existing is None
        or existing.recording_object != recording_ref
        or projection.recording_id != recording_ref.recording_id
        or projection.input_recording_digest != recording_ref.identity_digest()
        or source.schema is None
    ):
        raise ValueError("temporal input is not the exact recording")
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
    params: tuple[object, ...] = (
        projection.analysis_id,
        str(projection.recording_id),
        projection.input_recording_digest.algorithm.value,
        projection.input_recording_digest.value,
        source.artifact_id,
        source.digest.algorithm.value,
        source.digest.value,
        source.schema.schema_id,
        source.schema.version.major,
        source.schema.version.minor,
        projection.source_suite_request_digest.algorithm.value,
        projection.source_suite_request_digest.value,
        projection.request_digest.algorithm.value,
        projection.request_digest.value,
        bundle_ref.digest.algorithm.value,
        bundle_ref.digest.value,
        projection.stream_count,
        projection.probe_count,
        projection.point_count,
        idempotency_key,
    )
    try:
        cursor.execute(
            "SELECT public.publish_recording_starlink_temporal_pilot(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS published",
            params,
        )
        row = cursor.fetchone()
    except psycopg.errors.UniqueViolation as error:
        raise StarlinkTemporalPilotConflictError(
            "temporal catalog identity was reused"
        ) from error
    if row is None or row["published"] is not True:
        raise StarlinkTemporalPilotConflictError(
            "temporal publication was not acknowledged"
        )
    return StarlinkTemporalPilotProductRefV0_1(
        projection.analysis_id, projection.recording_id, bundle_ref
    )


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkTemporalPilotV0_1:
    source = ArtifactRef(
        str(row["source_suite_analysis_id"]),
        _digest(row, "source_suite_bundle"),
        SchemaRef(
            str(row["source_suite_schema_id"]),
            SchemaVersion(
                _integer(row["source_suite_schema_major"]),
                _integer(row["source_suite_schema_minor"]),
            ),
        ),
    )
    projection = StarlinkTemporalPilotCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        _digest(row, "input_recording"),
        _digest(row, "request"),
        source,
        _digest(row, "source_suite_request"),
        _integer(row["stream_count"]),
        _integer(row["probe_count"]),
        _integer(row["point_count"]),
    )
    ref = ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkTemporalPilotV0_1(projection, ref)


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
