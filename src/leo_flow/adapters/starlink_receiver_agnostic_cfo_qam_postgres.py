"""PostgreSQL catalog adapter for durable receiver-agnostic CFO/QAM v0.6."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
    CatalogedReceiverAgnosticCfoQamV0_6,
    ReceiverAgnosticCfoQamCatalogV0_6,
    ReceiverAgnosticCfoQamConflictError,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamCatalogProjectionV0_6,
    ReceiverAgnosticCfoQamRecordingProductRefV0_6,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresReceiverAgnosticCfoQamCatalogV0_6(ReceiverAgnosticCfoQamCatalogV0_6):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_receiver_agnostic_cfo_qam(
        self,
        projection: ReceiverAgnosticCfoQamCatalogProjectionV0_6,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6:
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise ReceiverAgnosticCfoQamConflictError(
                "receiver-agnostic CFO/QAM recording closure differs"
            )
        payload = {
            "analysis_id": projection.analysis_id,
            "recording_id": str(projection.recording_id),
            "recording_identity_digest_value": projection.recording_identity_digest.value,
            "request_digest_value": projection.request_digest.value,
            "bundle_digest_value": bundle_ref.digest.value,
            "stream_count": projection.stream_count,
            "window_count": projection.window_count,
            "pattern_evidence_count": projection.pattern_evidence_count,
            "unique_cell_count": projection.unique_cell_count,
            "pattern_evaluation_count": projection.pattern_evaluation_count,
            "candidates_only": projection.candidates_only,
            "idempotency_key": idempotency_key,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            published = PostgresRecordingCatalog.get_with_cursor(
                cursor, str(recording_ref.recording_id)
            )
            if published is None or published.recording_object != recording_ref:
                raise ReceiverAgnosticCfoQamConflictError(
                    "receiver-agnostic CFO/QAM input is not the exact recording"
                )
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_receiver_agnostic_cfo_qam_v0_6(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise ReceiverAgnosticCfoQamConflictError(
                "receiver-agnostic CFO/QAM publication was not acknowledged"
            )
        return ReceiverAgnosticCfoQamRecordingProductRefV0_6(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_receiver_agnostic_cfo_qam(
        self, ref: ReceiverAgnosticCfoQamRecordingProductRefV0_6
    ) -> CatalogedReceiverAgnosticCfoQamV0_6 | None:
        rows = self._read(
            "read_exact_recording_receiver_agnostic_cfo_qam_v0_6",
            ref.analysis_id,
            str(ref.recording_id),
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("receiver-agnostic CFO/QAM exact read is ambiguous")
        result = _cataloged(rows[0])
        return result if result.ref == ref else None

    def latest_receiver_agnostic_cfo_qam(
        self, recording_id: RecordingId
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6 | None:
        rows = self._read(
            "read_latest_recording_receiver_agnostic_cfo_qam_v0_6",
            str(recording_id),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("receiver-agnostic CFO/QAM latest read is ambiguous")
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
        raise ReceiverAgnosticCfoQamConflictError(
            "receiver-agnostic CFO/QAM object metadata conflicts"
        )


def _cataloged(row: dict[str, object]) -> CatalogedReceiverAgnosticCfoQamV0_6:
    projection = ReceiverAgnosticCfoQamCatalogProjectionV0_6(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        Digest(
            DigestAlgorithm.SHA256,
            str(row["recording_identity_digest_value"]),
        ),
        Digest(DigestAlgorithm.SHA256, str(row["request_digest_value"])),
        _integer(row["stream_count"]),
        _integer(row["window_count"]),
        _integer(row["pattern_evidence_count"]),
        _integer(row["unique_cell_count"]),
        _integer(row["pattern_evaluation_count"]),
        _boolean(row["candidates_only"]),
    )
    return CatalogedReceiverAgnosticCfoQamV0_6(projection, _blob(row, "bundle"))


def _blob(row: dict[str, object], prefix: str) -> ObjectRef:
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
            str(row[f"{prefix}_digest_value"]),
        ),
        _integer(row[f"{prefix}_byte_count"]),
        str(row[f"{prefix}_media_type"]),
        str(row[f"{prefix}_format_id"]),
        str(row[f"{prefix}_locator"]),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer database value")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean database value")
    return value
