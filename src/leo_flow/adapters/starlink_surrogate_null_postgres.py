"""Narrow PostgreSQL catalog for immutable Starlink surrogate-null evidence."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    CatalogedStarlinkSurrogateNullV0_1,
    StarlinkSurrogateNullConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullCatalogProjectionV0_1,
    StarlinkSurrogateNullProductRefV0_1,
    StarlinkSurrogateNullRecordingState,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkSurrogateNullCatalogV0_1:
    """Implements the v0.1 port through three fixed database routines."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_surrogate_null(
        self,
        projection: StarlinkSurrogateNullCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_starlink_surrogate_null_with_cursor(
                cursor,
                projection,
                bundle_ref,
                recording_ref,
                idempotency_key=idempotency_key,
            )

    def get_starlink_surrogate_null(
        self, ref: StarlinkSurrogateNullProductRefV0_1
    ) -> CatalogedStarlinkSurrogateNullV0_1 | None:
        result = self._get_exact(
            ref.analysis_id,
            ref.recording_id,
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        return result if result is not None and result.ref == ref else None

    def latest_starlink_surrogate_null(
        self, recording_id: RecordingId
    ) -> StarlinkSurrogateNullProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_surrogate_null(%s)",
                (str(recording_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        cataloged = self._get_exact(
            str(row["analysis_id"]),
            recording_id,
            str(row["bundle_digest_algorithm"]),
            str(row["bundle_digest_value"]),
        )
        if cataloged is None:
            raise RuntimeError("latest surrogate-null receipt cannot be resolved")
        return cataloged.ref

    def _get_exact(
        self,
        analysis_id: str,
        recording_id: RecordingId,
        bundle_algorithm: str,
        bundle_digest: str,
    ) -> CatalogedStarlinkSurrogateNullV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """SELECT *
                     FROM public.read_recording_starlink_surrogate_null(
                         %s,%s,%s,%s)""",
                (analysis_id, str(recording_id), bundle_algorithm, bundle_digest),
            )
            row = cursor.fetchone()
        return None if row is None else _cataloged(row)


def publish_starlink_surrogate_null_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: StarlinkSurrogateNullCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    recording_ref: RecordingObjectRef,
    *,
    idempotency_key: str,
) -> StarlinkSurrogateNullProductRefV0_1:
    """Publish exact surrogate evidence inside a caller-owned transaction."""

    if not idempotency_key:
        raise ValueError("idempotency_key cannot be empty")
    published = PostgresRecordingCatalog.get_with_cursor(
        cursor, str(recording_ref.recording_id)
    )
    if (
        published is None
        or published.recording_object != recording_ref
        or projection.recording_id != recording_ref.recording_id
        or projection.input_recording_digest != recording_ref.identity_digest()
    ):
        raise ValueError("surrogate-null input is not the exact recording")
    _register_live_object(cursor, bundle_ref)
    parameters = _parameters(projection, bundle_ref, idempotency_key=idempotency_key)
    try:
        cursor.execute(
            """SELECT public.publish_recording_starlink_surrogate_null(
            %(analysis_id)s,%(recording_id)s,
            %(input_algorithm)s,%(input_digest)s,
            %(source_analysis_id)s,%(source_bundle_algorithm)s,
            %(source_bundle_digest)s,%(source_schema_id)s,
            %(source_schema_major)s,%(source_schema_minor)s,
            %(source_request_algorithm)s,%(source_request_digest)s,
            %(request_algorithm)s,%(request_digest)s,
            %(bundle_algorithm)s,%(bundle_digest)s,%(state)s,
            %(stream_count)s,%(method_count)s,%(score_count)s,
            %(idempotency_key)s) AS published""",
            parameters,
        )
        row = cursor.fetchone()
    except psycopg.errors.UniqueViolation as error:
        raise StarlinkSurrogateNullConflictError(
            "surrogate-null catalog identity was reused"
        ) from error
    if row is None or row["published"] is not True:
        raise StarlinkSurrogateNullConflictError(
            "surrogate-null publication was not acknowledged"
        )
    return StarlinkSurrogateNullProductRefV0_1(
        projection.analysis_id, projection.recording_id, bundle_ref
    )


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
        """SELECT public.register_live_object_blob(
        %(algorithm)s,%(digest)s,%(bytes)s,%(media)s,%(format)s,%(locator)s)""",
        values,
    )
    cursor.execute(
        """SELECT byte_count,media_type,format_id,locator
             FROM public.object_blob
            WHERE digest_algorithm=%(algorithm)s
              AND digest_value=%(digest)s
              AND lifecycle_state='live'""",
        values,
    )
    row = cursor.fetchone()
    if row is None or (
        _integer(row["byte_count"]) != ref.byte_count
        or str(row["media_type"]) != ref.media_type
        or str(row["format_id"]) != ref.format_id
        or str(row["locator"]) != ref.locator
    ):
        raise StarlinkSurrogateNullConflictError(
            "surrogate-null object metadata conflicts"
        )


def _parameters(
    projection: StarlinkSurrogateNullCatalogProjectionV0_1,
    bundle_ref: ObjectRef,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    source = projection.source_suite_ref
    if source.schema is None:
        raise ValueError("source detector-suite schema is required")
    return {
        "analysis_id": projection.analysis_id,
        "recording_id": str(projection.recording_id),
        "input_algorithm": projection.input_recording_digest.algorithm.value,
        "input_digest": projection.input_recording_digest.value,
        "source_analysis_id": source.artifact_id,
        "source_bundle_algorithm": source.digest.algorithm.value,
        "source_bundle_digest": source.digest.value,
        "source_schema_id": source.schema.schema_id,
        "source_schema_major": source.schema.version.major,
        "source_schema_minor": source.schema.version.minor,
        "source_request_algorithm": (
            projection.source_suite_request_digest.algorithm.value
        ),
        "source_request_digest": projection.source_suite_request_digest.value,
        "request_algorithm": projection.request_digest.algorithm.value,
        "request_digest": projection.request_digest.value,
        "bundle_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest": bundle_ref.digest.value,
        "state": projection.state.value,
        "stream_count": projection.stream_count,
        "method_count": projection.method_count,
        "score_count": projection.surrogate_score_count,
        "idempotency_key": idempotency_key,
    }


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkSurrogateNullV0_1:
    source_schema = SchemaRef(
        str(row["source_suite_schema_id"]),
        SchemaVersion(
            _integer(row["source_suite_schema_major"]),
            _integer(row["source_suite_schema_minor"]),
        ),
    )
    projection = StarlinkSurrogateNullCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        _digest(row, "input_recording"),
        ArtifactRef(
            str(row["source_suite_analysis_id"]),
            _digest(row, "source_suite_bundle"),
            source_schema,
        ),
        _digest(row, "source_suite_request"),
        _digest(row, "request"),
        StarlinkSurrogateNullRecordingState(str(row["result_state"])),
        _integer(row["stream_count"]),
        _integer(row["method_count"]),
        _integer(row["surrogate_score_count"]),
    )
    bundle_ref = ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkSurrogateNullV0_1(projection, bundle_ref)


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
