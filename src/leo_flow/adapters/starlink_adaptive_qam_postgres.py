"""PostgreSQL catalog adapter for adaptive QAM v0.4 products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    CatalogedStarlinkAdaptiveQamV0_4,
    StarlinkAdaptiveQamCatalogV0_4,
    StarlinkAdaptiveQamConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    StarlinkAdaptiveQamCatalogProjectionV0_4,
    StarlinkAdaptiveQamProductRefV0_4,
)
from leo_flow.contracts.starlink_adaptive_response import (
    V0_1 as ADAPTIVE_RESPONSE_V0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkAdaptiveQamCatalogV0_4(StarlinkAdaptiveQamCatalogV0_4):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_adaptive_qam(
        self,
        projection: StarlinkAdaptiveQamCatalogProjectionV0_4,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveQamProductRefV0_4:
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise StarlinkAdaptiveQamConflictError(
                "adaptive QAM recording closure differs"
            )
        payload = {
            "analysis_id": projection.analysis_id,
            "recording_id": str(projection.recording_id),
            "input_recording_digest_value": projection.recording_identity_digest.value,
            "source_adaptive_response_analysis_id": projection.source_adaptive_response_ref.artifact_id,
            "source_adaptive_response_bundle_digest_value": projection.source_adaptive_response_ref.digest.value,
            "source_suite_analysis_id": projection.source_suite_ref.artifact_id,
            "source_suite_bundle_digest_value": projection.source_suite_ref.digest.value,
            "request_digest_value": projection.request_digest.value,
            "bundle_digest_value": bundle_ref.digest.value,
            "stream_count": projection.stream_count,
            "window_count": projection.window_count,
            "point_count": projection.point_count,
            "idempotency_key": idempotency_key,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_starlink_adaptive_qam_v0_4(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise StarlinkAdaptiveQamConflictError(
                "adaptive QAM publication was not acknowledged"
            )
        return StarlinkAdaptiveQamProductRefV0_4(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_adaptive_qam(
        self, ref: StarlinkAdaptiveQamProductRefV0_4
    ) -> CatalogedStarlinkAdaptiveQamV0_4 | None:
        rows = self._read(
            "read_exact_recording_starlink_adaptive_qam_v0_4",
            ref.analysis_id,
            str(ref.recording_id),
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("adaptive QAM exact read is ambiguous")
        return _cataloged(rows[0])

    def latest_starlink_adaptive_qam(
        self, recording_id: RecordingId
    ) -> StarlinkAdaptiveQamProductRefV0_4 | None:
        rows = self._read(
            "read_latest_recording_starlink_adaptive_qam_v0_4", str(recording_id)
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("adaptive QAM latest read is ambiguous")
        return _cataloged(rows[0]).ref

    def _read(self, function: str, *values: object) -> list[dict[str, object]]:
        placeholders = ",".join("%s" for _value in values)
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
        "SELECT byte_count,media_type,format_id,locator "
        "FROM public.object_blob "
        "WHERE digest_algorithm=%s AND digest_value=%s "
        "AND lifecycle_state='live'",
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
        raise StarlinkAdaptiveQamConflictError("adaptive QAM object metadata conflicts")


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkAdaptiveQamV0_4:
    projection = StarlinkAdaptiveQamCatalogProjectionV0_4(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        Digest(DigestAlgorithm.SHA256, str(row["input_recording_digest_value"])),
        ArtifactRef(
            str(row["source_adaptive_response_analysis_id"]),
            Digest(
                DigestAlgorithm.SHA256,
                str(row["source_adaptive_response_bundle_digest_value"]),
            ),
            SchemaRef(
                StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID,
                ADAPTIVE_RESPONSE_V0_1,
            ),
        ),
        ArtifactRef(
            str(row["source_suite_analysis_id"]),
            Digest(
                DigestAlgorithm.SHA256,
                str(row["source_suite_bundle_digest_value"]),
            ),
            SchemaRef(
                StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID,
                SchemaVersion(0, 2),
            ),
        ),
        Digest(DigestAlgorithm.SHA256, str(row["request_digest_value"])),
        _integer(row["stream_count"]),
        _integer(row["window_count"]),
        _integer(row["point_count"]),
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
    return CatalogedStarlinkAdaptiveQamV0_4(projection, blob)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer database value")
    return value
