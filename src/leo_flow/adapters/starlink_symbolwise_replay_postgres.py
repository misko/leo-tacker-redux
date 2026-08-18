"""PostgreSQL catalog and fenced explicit queue for symbolwise replay."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_symbolwise_replay_product_codec import (
    decode_starlink_symbolwise_replay_request,
    encode_starlink_symbolwise_replay_request,
)
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
    CatalogedStarlinkSymbolwiseReplayV0_1,
    StarlinkSymbolwiseReplayConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    StarlinkSymbolwiseRecordingProductRefV0_1,
    StarlinkSymbolwiseReplayCatalogProjectionV0_1,
    StarlinkSymbolwiseReplayPublicationFenceV0_1,
    StarlinkSymbolwiseReplayRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.starlink_symbolwise_replay_product import (
    StaleStarlinkSymbolwiseReplayLeaseError,
    StarlinkSymbolwiseReplayWorkLeaseV0_1,
)
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkSymbolwiseReplayRepositoryV0_1:
    """One analysis-role adapter owns catalog and explicitly admitted work."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: uuid.uuid4().hex)

    def enqueue(
        self,
        request: StarlinkSymbolwiseReplayRequestV0_1,
        *,
        priority: int = 0,
        idempotency_key: str,
    ) -> str:
        if not 0 <= priority <= 100 or not idempotency_key:
            raise ValueError("symbolwise replay enqueue bounds are invalid")
        work_id = (
            "slsymwork_"
            + canonical_digest(
                {
                    "recording_identity": request.recording_object_ref.identity_digest(),
                    "request_digest": request.digest,
                }
            ).value[:32]
        )
        request_json = json.loads(encode_starlink_symbolwise_replay_request(request))
        payload = {
            "work_id": work_id,
            "recording_id": str(request.recording_id),
            "recording_identity_digest_value": request.recording_object_ref.identity_digest().value,
            "request_digest_value": request.digest.value,
            "request_json": request_json,
            "priority": priority,
            "idempotency_key": idempotency_key,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                "SELECT public.enqueue_starlink_symbolwise_replay_work_v0_1(%s) AS work_id",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or str(row["work_id"]) != work_id:
            raise StarlinkSymbolwiseReplayConflictError(
                "symbolwise replay enqueue was not acknowledged"
            )
        return work_id

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkSymbolwiseReplayWorkLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("symbolwise replay claim bounds are invalid")
        token = f"{worker_id}:slsymlease_{self._token()}"
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            rows = cursor.execute(
                "SELECT * FROM public.claim_starlink_symbolwise_replay_work_v0_1(%s,%s)",
                (token, timedelta(seconds=lease_ttl_s)),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("symbolwise replay claim is ambiguous")
        row = rows[0]
        request = decode_starlink_symbolwise_replay_request(
            canonical_json_bytes(row["request_json"])
        )
        recording_ref = _recording_ref(row)
        if (
            request.recording_object_ref != recording_ref
            or request.digest.value != str(row["request_digest_value"])
            or recording_ref.identity_digest().value
            != str(row["recording_identity_digest_value"])
        ):
            raise RuntimeError("symbolwise replay claim source closure differs")
        return StarlinkSymbolwiseReplayWorkLeaseV0_1(
            str(row["work_id"]),
            request,
            recording_ref,
            str(row["lease_token"]),
            _integer(row["lease_generation"]),
            _integer(row["attempt"]),
        )

    def complete(
        self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, result: ArtifactRef
    ) -> None:
        self._transition(
            "complete_starlink_symbolwise_replay_work_v0_1",
            lease,
            result.artifact_id,
            result.digest.value,
        )

    def retry(self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, reason: str) -> None:
        self._transition("retry_starlink_symbolwise_replay_work_v0_1", lease, reason)

    def park(self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, reason: str) -> None:
        self._transition("park_starlink_symbolwise_replay_work_v0_1", lease, reason)

    def _transition(
        self,
        function: str,
        lease: StarlinkSymbolwiseReplayWorkLeaseV0_1,
        *extra: object,
    ) -> None:
        placeholders = ",%s" * len(extra)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                f"SELECT public.{function}(%s,%s,%s{placeholders}) AS changed",
                (
                    lease.work_id,
                    lease.lease_token,
                    lease.lease_generation,
                    *extra,
                ),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise StaleStarlinkSymbolwiseReplayLeaseError(
                "symbolwise replay work lease is stale"
            )

    def publish_starlink_symbolwise_replay(
        self,
        projection: StarlinkSymbolwiseReplayCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        lease_fence: StarlinkSymbolwiseReplayPublicationFenceV0_1,
        idempotency_key: str,
    ) -> StarlinkSymbolwiseRecordingProductRefV0_1:
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise StarlinkSymbolwiseReplayConflictError(
                "symbolwise replay recording closure differs"
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
            "candidates_only": projection.candidates_only,
            "work_id": lease_fence.work_id,
            "lease_token": lease_fence.lease_token,
            "lease_generation": lease_fence.lease_generation,
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
                raise StarlinkSymbolwiseReplayConflictError(
                    "symbolwise replay input is not the exact recording"
                )
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_starlink_symbolwise_replay_v0_1(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise StarlinkSymbolwiseReplayConflictError(
                "symbolwise replay publication was not acknowledged"
            )
        return StarlinkSymbolwiseRecordingProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_symbolwise_replay(
        self, ref: StarlinkSymbolwiseRecordingProductRefV0_1
    ) -> CatalogedStarlinkSymbolwiseReplayV0_1 | None:
        rows = self._read(
            "read_exact_recording_starlink_symbolwise_replay_v0_1",
            ref.analysis_id,
            str(ref.recording_id),
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("symbolwise replay exact read is ambiguous")
        result = _cataloged(rows[0])
        return result if result.ref == ref else None

    def latest_starlink_symbolwise_replay(
        self, recording_id: RecordingId
    ) -> StarlinkSymbolwiseRecordingProductRefV0_1 | None:
        rows = self._read(
            "read_latest_recording_starlink_symbolwise_replay_v0_1",
            str(recording_id),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("symbolwise replay latest read is ambiguous")
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
        raise StarlinkSymbolwiseReplayConflictError(
            "symbolwise replay object metadata conflicts"
        )


def _recording_ref(row: dict[str, object]) -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId(str(row["recording_id"])),
        _blob(row, "data"),
        _blob(row, "metadata"),
        Digest(DigestAlgorithm.SHA256, str(row["recording_manifest_digest_value"])),
    )


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkSymbolwiseReplayV0_1:
    projection = StarlinkSymbolwiseReplayCatalogProjectionV0_1(
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
        _boolean(row["candidates_only"]),
    )
    return CatalogedStarlinkSymbolwiseReplayV0_1(projection, _blob(row, "bundle"))


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
    if isinstance(value, bool):
        raise TypeError("database integer cannot be boolean")
    return int(str(value))


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("database boolean is invalid")
    return value
