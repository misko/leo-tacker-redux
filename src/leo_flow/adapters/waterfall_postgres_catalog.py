"""PostgreSQL catalog for immutable bounded waterfall products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.waterfall_persistence import (
    CatalogedWaterfallV0_1,
    WaterfallCatalogProjectionV0_1,
)
from leo_flow.contracts.core import AnalysisRunId, Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall import WaterfallProductId, WaterfallProductRefV0_1
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]

_SELECT = """
SELECT w.*, o.byte_count AS bundle_byte_count,
       o.media_type AS bundle_media_type, o.format_id AS bundle_format_id,
       o.locator AS bundle_locator
FROM public.recording_waterfall AS w
JOIN public.object_blob AS o ON (o.digest_algorithm,o.digest_value)=
    (w.bundle_digest_algorithm,w.bundle_digest_value)
WHERE o.lifecycle_state='live'
"""


class PostgresWaterfallCatalogError(RuntimeError):
    pass


class WaterfallCatalogConflictError(PostgresWaterfallCatalogError):
    pass


class WaterfallRecordingMismatchError(PostgresWaterfallCatalogError):
    pass


class PostgresWaterfallCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_waterfall(
        self,
        projection: WaterfallCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> WaterfallProductRefV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_waterfall_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_waterfall(
        self, ref: WaterfallProductRefV0_1
    ) -> CatalogedWaterfallV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                _SELECT
                + """ AND w.product_id=%s AND w.analysis_run_id=%s
                      AND w.recording_id=%s AND w.bundle_digest_algorithm=%s
                      AND w.bundle_digest_value=%s""",
                (
                    str(ref.product_id),
                    str(ref.analysis_run_id),
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


def publish_waterfall_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: WaterfallCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> WaterfallProductRefV0_1:
    if not idempotency_key:
        raise ValueError("idempotency_key cannot be empty")
    existing_recording = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    if (
        existing_recording is None
        or existing_recording.recording_object != recording_ref
        or projection.recording_id != str(recording_ref.recording_id)
        or projection.input_recording_digest != recording_ref.identity_digest()
    ):
        raise WaterfallRecordingMismatchError(
            "waterfall input is not the exact published recording"
        )
    _register_object(cursor, bundle_ref)
    parameters = _parameters(projection, bundle_ref, idempotency_key)
    cursor.execute(
        """SELECT public.publish_recording_waterfall(
        %(product_id)s,%(analysis_run_id)s,%(recording_id)s,
        %(input_digest_algorithm)s,%(input_digest_value)s,
        %(request_digest_algorithm)s,%(request_digest_value)s,
        %(bundle_digest_algorithm)s,%(bundle_digest_value)s,
        %(tile_count)s,%(cell_count)s,%(idempotency_key)s) AS inserted""",
        parameters,
    )
    inserted = cursor.fetchone()
    if inserted is not None and inserted["inserted"] is True:
        return _ref(projection, bundle_ref)
    cursor.execute(
        _SELECT
        + """ AND (w.product_id=%(product_id)s OR w.analysis_run_id=%(analysis_run_id)s
          OR w.idempotency_key=%(idempotency_key)s
          OR (w.recording_id,w.input_recording_digest_algorithm,
              w.input_recording_digest_value,w.request_digest_algorithm,
              w.request_digest_value)=(%(recording_id)s,%(input_digest_algorithm)s,
              %(input_digest_value)s,%(request_digest_algorithm)s,%(request_digest_value)s))""",
        parameters,
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise WaterfallCatalogConflictError("waterfall identities conflict")
    existing = _cataloged(rows[0])
    if (
        rows[0]["idempotency_key"] != idempotency_key
        or existing.projection != projection
        or existing.bundle_ref != bundle_ref
    ):
        raise WaterfallCatalogConflictError("waterfall identity was reused")
    return existing.ref


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
        raise WaterfallCatalogConflictError("waterfall object metadata conflicts")


def _parameters(
    projection: WaterfallCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "product_id": projection.product_id,
        "analysis_run_id": projection.analysis_run_id,
        "recording_id": projection.recording_id,
        "input_digest_algorithm": projection.input_recording_digest.algorithm.value,
        "input_digest_value": projection.input_recording_digest.value,
        "request_digest_algorithm": projection.request_digest.algorithm.value,
        "request_digest_value": projection.request_digest.value,
        "bundle_digest_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest_value": bundle_ref.digest.value,
        "tile_count": projection.tile_count,
        "cell_count": projection.cell_count,
        "idempotency_key": idempotency_key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedWaterfallV0_1:
    projection = WaterfallCatalogProjectionV0_1(
        str(row["product_id"]),
        str(row["analysis_run_id"]),
        str(row["recording_id"]),
        _digest(row, "input_recording"),
        _digest(row, "request"),
        _integer(row["tile_count"], "tile_count"),
        _integer(row["cell_count"], "cell_count"),
    )
    bundle = ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"], "bundle_byte_count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedWaterfallV0_1(projection, bundle)


def _ref(
    projection: WaterfallCatalogProjectionV0_1, bundle: ObjectRef
) -> WaterfallProductRefV0_1:
    return WaterfallProductRefV0_1(
        WaterfallProductId(projection.product_id),
        AnalysisRunId(projection.analysis_run_id),
        RecordingId(projection.recording_id),
        bundle,
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresWaterfallCatalogError(f"database {name} is not an integer")
    return value
