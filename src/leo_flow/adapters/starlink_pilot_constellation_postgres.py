"""PostgreSQL catalog adapter for pilot constellation products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
    CatalogedStarlinkPilotConstellationV0_1,
    StarlinkPilotConstellationConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationCatalogProjectionV0_1,
    StarlinkPilotConstellationProductRefV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkPilotConstellationCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_pilot_constellation(
        self,
        projection: StarlinkPilotConstellationCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotConstellationProductRefV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_pilot_constellation_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink_pilot_constellation(
        self, ref: StarlinkPilotConstellationProductRefV0_1
    ) -> CatalogedStarlinkPilotConstellationV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_recording_starlink_pilot_constellation(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else _cataloged(row)

    def latest_starlink_pilot_constellation(
        self, recording_id: RecordingId
    ) -> StarlinkPilotConstellationProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_pilot_constellation(%s)",
                (str(recording_id),),
            )
            latest = cursor.fetchone()
            if latest is None:
                return None
            cursor.execute(
                "SELECT * FROM public.read_recording_starlink_pilot_constellation(%s,%s,%s,%s)",
                (
                    latest["analysis_id"],
                    str(recording_id),
                    latest["bundle_digest_algorithm"],
                    latest["bundle_digest_value"],
                ),
            )
            row = cursor.fetchone()
        return None if row is None else _cataloged(row).ref


def publish_starlink_pilot_constellation_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkPilotConstellationCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkPilotConstellationProductRefV0_1:
    """Publish or exactly replay within a caller-owned transaction."""
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
        raise ValueError("constellation input is not the exact recording")
    _register(cursor, bundle_ref)
    params = _parameters(projection, bundle_ref, idempotency_key)
    try:
        cursor.execute(
            "SELECT public.publish_recording_starlink_pilot_constellation(%(analysis_id)s,%(recording_id)s,%(input_algorithm)s,%(input_digest)s,%(source_analysis_id)s,%(source_bundle_algorithm)s,%(source_bundle_digest)s,%(source_schema_id)s,%(source_schema_major)s,%(source_schema_minor)s,%(source_request_algorithm)s,%(source_request_digest)s,%(request_algorithm)s,%(request_digest)s,%(bundle_algorithm)s,%(bundle_digest)s,%(stream_count)s,%(point_count)s,%(idempotency_key)s) AS published",
            params,
        )
        row = cursor.fetchone()
    except psycopg.errors.UniqueViolation as error:
        raise StarlinkPilotConstellationConflictError(
            "constellation catalog identity was reused"
        ) from error
    if row is None or row["published"] is not True:
        raise StarlinkPilotConstellationConflictError(
            "constellation publication was not acknowledged"
        )
    return StarlinkPilotConstellationProductRefV0_1(
        projection.analysis_id, projection.recording_id, bundle_ref
    )


def _register(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
    values = {
        "a": ref.digest.algorithm.value,
        "d": ref.digest.value,
        "b": ref.byte_count,
        "m": ref.media_type,
        "f": ref.format_id,
        "l": ref.locator,
    }
    cursor.execute(
        "SELECT public.register_live_object_blob(%(a)s,%(d)s,%(b)s,%(m)s,%(f)s,%(l)s)",
        values,
    )


def _parameters(
    p: StarlinkPilotConstellationCatalogProjectionV0_1, ref: ObjectRef, key: str
) -> dict[str, object]:
    source = p.source_suite_ref
    if source.schema is None:
        raise ValueError("source suite schema is required")
    return {
        "analysis_id": p.analysis_id,
        "recording_id": str(p.recording_id),
        "input_algorithm": p.input_recording_digest.algorithm.value,
        "input_digest": p.input_recording_digest.value,
        "source_analysis_id": source.artifact_id,
        "source_bundle_algorithm": source.digest.algorithm.value,
        "source_bundle_digest": source.digest.value,
        "source_schema_id": source.schema.schema_id,
        "source_schema_major": source.schema.version.major,
        "source_schema_minor": source.schema.version.minor,
        "source_request_algorithm": p.source_suite_request_digest.algorithm.value,
        "source_request_digest": p.source_suite_request_digest.value,
        "request_algorithm": p.request_digest.algorithm.value,
        "request_digest": p.request_digest.value,
        "bundle_algorithm": ref.digest.algorithm.value,
        "bundle_digest": ref.digest.value,
        "stream_count": p.stream_count,
        "point_count": p.point_count,
        "idempotency_key": key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkPilotConstellationV0_1:
    source = ArtifactRef(
        str(row["source_suite_analysis_id"]),
        _digest(row, "source_suite_bundle"),
        SchemaRef(
            str(row["source_suite_schema_id"]),
            SchemaVersion(
                _int(row["source_suite_schema_major"]),
                _int(row["source_suite_schema_minor"]),
            ),
        ),
    )
    projection = StarlinkPilotConstellationCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        _digest(row, "input_recording"),
        source,
        _digest(row, "source_suite_request"),
        _digest(row, "request"),
        _int(row["stream_count"]),
        _int(row["point_count"]),
    )
    ref = ObjectRef(
        _digest(row, "bundle"),
        _int(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkPilotConstellationV0_1(projection, ref)


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
