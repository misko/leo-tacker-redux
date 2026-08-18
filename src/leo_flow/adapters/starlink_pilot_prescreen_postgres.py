"""PostgreSQL catalog adapter for complete-IQ pilot-prescreen products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_pilot_prescreen_persistence import (
    CatalogedStarlinkPilotPrescreenV0_1,
    StarlinkPilotPrescreenCatalogProjectionV0_1,
    StarlinkPilotPrescreenCatalogV0_1,
    StarlinkPilotPrescreenConflictError,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenProductRefV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkPilotPrescreenCatalogV0_1(StarlinkPilotPrescreenCatalogV0_1):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_pilot_prescreen(
        self,
        projection: StarlinkPilotPrescreenCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotPrescreenProductRefV0_1:
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise StarlinkPilotPrescreenConflictError(
                "pilot-prescreen recording closure differs"
            )
        payload = {
            "analysis_id": projection.analysis_id,
            "recording_id": str(projection.recording_id),
            "recording_identity_digest_value": projection.recording_identity_digest.value,
            "request_digest_value": projection.request_digest.value,
            "data_digest_value": recording_ref.data_object.digest.value,
            "metadata_digest_value": recording_ref.metadata_object.digest.value,
            "manifest_digest_value": recording_ref.manifest_digest.value,
            "bundle_digest_value": bundle_ref.digest.value,
            "stream_count": projection.stream_count,
            "window_count": projection.window_count,
            "analyzed_sample_count": projection.analyzed_sample_count,
            "selected_window_count": projection.selected_window_count,
            "idempotency_key": idempotency_key,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_starlink_pilot_prescreen_v0_1(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise StarlinkPilotPrescreenConflictError(
                "pilot-prescreen publication was not acknowledged"
            )
        return StarlinkPilotPrescreenProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_pilot_prescreen(
        self, ref: StarlinkPilotPrescreenProductRefV0_1
    ) -> CatalogedStarlinkPilotPrescreenV0_1 | None:
        rows = self._read(
            "read_exact_recording_starlink_pilot_prescreen_v0_1",
            ref.analysis_id,
            str(ref.recording_id),
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("pilot-prescreen exact read is ambiguous")
        return _cataloged(rows[0])

    def latest_starlink_pilot_prescreen(
        self, recording_id: RecordingId
    ) -> StarlinkPilotPrescreenProductRefV0_1 | None:
        rows = self._read(
            "read_latest_recording_starlink_pilot_prescreen_v0_1", str(recording_id)
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("pilot-prescreen latest read is ambiguous")
        return _cataloged(rows[0]).ref

    def _read(self, function: str, *values: object) -> list[dict[str, object]]:
        placeholders = ",".join("%s" for _ in values)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            return cursor.execute(
                f"SELECT * FROM public.{function}({placeholders})", values
            ).fetchall()


def _register_live_object(
    cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef
) -> None:
    values = (
        ref.digest.algorithm.value,
        ref.digest.value,
        ref.byte_count,
        ref.media_type,
        ref.format_id,
        ref.locator,
    )
    cursor.execute("SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)", values)
    row = cursor.execute(
        "SELECT byte_count,media_type,format_id,locator FROM public.object_blob "
        "WHERE digest_algorithm=%s AND digest_value=%s AND lifecycle_state='live'",
        values[:2],
    ).fetchone()
    if (
        row is None
        or (
            _integer(row["byte_count"]),
            str(row["media_type"]),
            str(row["format_id"]),
            str(row["locator"]),
        )
        != values[2:]
    ):
        raise StarlinkPilotPrescreenConflictError(
            "pilot-prescreen object metadata conflicts"
        )


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkPilotPrescreenV0_1:
    projection = StarlinkPilotPrescreenCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        Digest(DigestAlgorithm.SHA256, str(row["recording_identity_digest_value"])),
        Digest(DigestAlgorithm.SHA256, str(row["request_digest_value"])),
        _integer(row["stream_count"]),
        _integer(row["window_count"]),
        _integer(row["analyzed_sample_count"]),
        _integer(row["selected_window_count"]),
    )
    blob = ObjectRef(
        Digest(
            DigestAlgorithm(str(row["bundle_digest_algorithm"])),
            str(row["bundle_digest_value"]),
        ),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkPilotPrescreenV0_1(projection, blob)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer database value")
    return value
