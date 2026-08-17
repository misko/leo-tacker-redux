"""PostgreSQL catalog for immutable Starlink candidate bundles."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_persistence import (
    CatalogedStarlinkV0_1,
    StarlinkCatalogProjectionV0_1,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.starlink_pipeline import StarlinkPilotAnalysisProductRefV0_1
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]

_SELECT = """
SELECT s.*,o.byte_count AS bundle_byte_count,o.media_type AS bundle_media_type,
       o.format_id AS bundle_format_id,o.locator AS bundle_locator
  FROM public.recording_starlink_candidate AS s
  JOIN public.object_blob AS o ON (o.digest_algorithm,o.digest_value)=
       (s.bundle_digest_algorithm,s.bundle_digest_value)
 WHERE o.lifecycle_state='live'
"""


class StarlinkCatalogConflictError(RuntimeError):
    pass


class StarlinkRecordingMismatchError(RuntimeError):
    pass


class PostgresStarlinkCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink(
        self,
        projection: StarlinkCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotAnalysisProductRefV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink(
        self, ref: StarlinkPilotAnalysisProductRefV0_1
    ) -> CatalogedStarlinkV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                _SELECT
                + """ AND s.analysis_id=%s AND s.recording_id=%s
                  AND s.bundle_digest_algorithm=%s AND s.bundle_digest_value=%s""",
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


def publish_starlink_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkPilotAnalysisProductRefV0_1:
    if not idempotency_key:
        raise ValueError("idempotency_key cannot be empty")
    existing = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    if (
        existing is None
        or existing.recording_object != recording_ref
        or projection.recording_id != str(recording_ref.recording_id)
        or projection.input_recording_digest != recording_ref.identity_digest()
    ):
        raise StarlinkRecordingMismatchError(
            "Starlink input is not the exact published recording"
        )
    _register_object(cursor, bundle_ref)
    values = _parameters(projection, bundle_ref, idempotency_key)
    cursor.execute(
        """SELECT public.publish_recording_starlink_candidate(
        %(analysis_id)s,%(recording_id)s,%(input_algorithm)s,%(input_digest)s,
        %(request_algorithm)s,%(request_digest)s,%(bundle_algorithm)s,
        %(bundle_digest)s,%(candidate_count)s,%(stream_count)s,
        %(idempotency_key)s) AS inserted""",
        values,
    )
    inserted = cursor.fetchone()
    if inserted is not None and inserted["inserted"] is True:
        return _ref(projection, bundle_ref)
    cursor.execute(
        _SELECT
        + """ AND (s.analysis_id=%(analysis_id)s
          OR s.idempotency_key=%(idempotency_key)s
          OR (s.recording_id,s.input_recording_digest_algorithm,
              s.input_recording_digest_value,s.request_digest_algorithm,
              s.request_digest_value)=(%(recording_id)s,%(input_algorithm)s,
              %(input_digest)s,%(request_algorithm)s,%(request_digest)s))""",
        values,
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise StarlinkCatalogConflictError("Starlink identities conflict")
    cataloged = _cataloged(rows[0])
    if (
        rows[0]["idempotency_key"] != idempotency_key
        or cataloged.projection != projection
        or cataloged.bundle_ref != bundle_ref
    ):
        raise StarlinkCatalogConflictError("Starlink identity was reused")
    return cataloged.ref


def _register_object(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
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
        row["byte_count"] != ref.byte_count
        or row["media_type"] != ref.media_type
        or row["format_id"] != ref.format_id
        or row["locator"] != ref.locator
    ):
        raise StarlinkCatalogConflictError("Starlink object metadata conflicts")


def _parameters(
    p: StarlinkCatalogProjectionV0_1, b: ObjectRef, key: str
) -> dict[str, object]:
    return {
        "analysis_id": p.analysis_id,
        "recording_id": p.recording_id,
        "input_algorithm": p.input_recording_digest.algorithm.value,
        "input_digest": p.input_recording_digest.value,
        "request_algorithm": p.request_digest.algorithm.value,
        "request_digest": p.request_digest.value,
        "bundle_algorithm": b.digest.algorithm.value,
        "bundle_digest": b.digest.value,
        "candidate_count": p.candidate_count,
        "stream_count": p.analyzed_stream_count,
        "idempotency_key": key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkV0_1:
    projection = StarlinkCatalogProjectionV0_1(
        str(row["analysis_id"]),
        str(row["recording_id"]),
        _digest(row, "input_recording"),
        _digest(row, "request"),
        _integer(row["candidate_count"]),
        _integer(row["analyzed_stream_count"]),
    )
    bundle = ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkV0_1(projection, bundle)


def _ref(
    p: StarlinkCatalogProjectionV0_1, b: ObjectRef
) -> StarlinkPilotAnalysisProductRefV0_1:
    return StarlinkPilotAnalysisProductRefV0_1(
        p.analysis_id, RecordingId(p.recording_id), b
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database Starlink count is invalid")
    return value
