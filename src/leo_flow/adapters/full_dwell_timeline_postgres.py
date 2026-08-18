"""PostgreSQL catalog and fenced work adapters for prompt IQ timelines."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    CatalogedFullDwellTimelineV0_1,
    FullDwellTimelineCatalogProjectionV0_1,
    FullDwellTimelineConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellRefinementRequestV0_1,
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineProductRefV0_1,
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.full_dwell_timeline import FullDwellTimelineLeaseV0_1
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class StaleFullDwellTimelineLeaseError(RuntimeError):
    pass


class PostgresFullDwellTimelineCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_full_dwell_timeline(
        self,
        projection: FullDwellTimelineCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FullDwellTimelineProductRefV0_1:
        if not idempotency_key:
            raise ValueError("idempotency key cannot be empty")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            published = PostgresRecordingCatalog.get_with_cursor(
                cursor, str(recording_ref.recording_id)
            )
            if published is None or published.recording_object != recording_ref:
                raise ValueError("timeline input is not the exact recording")
            _register_live_object(cursor, bundle_ref)
            try:
                row = cursor.execute(
                    "SELECT public.publish_recording_full_dwell_timeline_v0_1(%s) AS published",
                    (
                        Jsonb(
                            _publication(
                                projection,
                                bundle_ref,
                                recording_ref,
                                idempotency_key,
                            )
                        ),
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise FullDwellTimelineConflictError(
                    "timeline catalog identity was reused"
                ) from error
            if row is None or row["published"] is not True:
                raise FullDwellTimelineConflictError(
                    "timeline publication was not acknowledged"
                )
        return FullDwellTimelineProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_full_dwell_timeline(
        self, ref: FullDwellTimelineProductRefV0_1
    ) -> CatalogedFullDwellTimelineV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            row = cursor.execute(
                "SELECT * FROM public.read_exact_recording_full_dwell_timeline_v0_1(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            ).fetchone()
        cataloged = None if row is None else _cataloged(row)
        return cataloged if cataloged is not None and cataloged.ref == ref else None

    def latest_full_dwell_timeline(
        self, recording_id: RecordingId
    ) -> FullDwellTimelineProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            row = cursor.execute(
                "SELECT * FROM public.read_latest_recording_full_dwell_timeline_v0_1(%s)",
                (str(recording_id),),
            ).fetchone()
        return None if row is None else _cataloged(row).ref


class PostgresFullDwellTimelineWorkRepositoryV0_1:
    """Lease requests admitted independently by the bounded backfill operator."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        lease_ttl_s: float = 900.0,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not 1 <= lease_ttl_s <= 28_800:
            raise ValueError("timeline lease TTL is outside its bound")
        self._connect, self._lease_ttl_s = connect, lease_ttl_s
        self._token = token_factory or (lambda: uuid.uuid4().hex)

    def newest_candidate_ids(self, maximum: int) -> tuple[RecordingId, ...]:
        if not 1 <= maximum <= 64:
            raise ValueError("timeline candidate bound is invalid")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                "SELECT * FROM public.list_full_dwell_timeline_candidates_v0_1(%s)",
                (maximum,),
            ).fetchall()
        return tuple(RecordingId(str(row["recording_id"])) for row in rows)

    def receiver_lnbs(
        self, recording_id: RecordingId, at_utc_ns: int
    ) -> dict[ReceiverChainId, tuple[RadioId, str]]:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                "SELECT * FROM public.read_full_dwell_timeline_hardware_v0_1(%s,%s)",
                (str(recording_id), at_utc_ns),
            ).fetchall()
        result: dict[ReceiverChainId, tuple[RadioId, str]] = {}
        for row in rows:
            receiver = ReceiverChainId(str(row["receiver_chain_id"]))
            if receiver in result:
                raise RuntimeError("timeline hardware mapping is ambiguous")
            result[receiver] = (RadioId(str(row["radio_id"])), str(row["lnb_id"]))
        if not result:
            raise LookupError("recording has no authoritative hardware mapping")
        return result

    def admit(
        self, recording_ref: RecordingObjectRef, request: dict[str, object]
    ) -> bool:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            published = PostgresRecordingCatalog.get_with_cursor(
                cursor, str(recording_ref.recording_id)
            )
            if published is None or published.recording_object != recording_ref:
                raise ValueError("timeline admission is not source-closed")
            row = cursor.execute(
                "SELECT public.admit_full_dwell_timeline_work_v0_1(%s,%s) AS admitted",
                (Jsonb(_recording_ref_json(recording_ref)), Jsonb(request)),
            ).fetchone()
        return row is not None and row["admitted"] is True

    def claim(self, worker_id: str) -> FullDwellTimelineLeaseV0_1 | None:
        if not worker_id:
            raise ValueError("worker identity cannot be empty")
        token = f"{worker_id}:{self._token()}"
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                "SELECT * FROM public.claim_full_dwell_timeline_work_v0_1(%s,%s)",
                (token, timedelta(seconds=self._lease_ttl_s)),
            ).fetchone()
        return None if row is None else _lease(row)

    def complete_timeline(
        self, lease: FullDwellTimelineLeaseV0_1, result: ArtifactRef
    ) -> None:
        self._transition(
            "complete_full_dwell_timeline_work_v0_1",
            lease,
            result.artifact_id,
            result.digest.value,
        )

    def retry_timeline(self, lease: FullDwellTimelineLeaseV0_1, reason: str) -> None:
        self._transition("retry_full_dwell_timeline_work_v0_1", lease, reason)

    def record_refinement_dispatch_failure(
        self, lease: FullDwellTimelineLeaseV0_1, reason: str
    ) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                "SELECT public.fail_full_dwell_refinement_dispatch_v0_1(%s,%s) AS changed",
                (lease.work_id, reason),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise RuntimeError("refinement dispatch failure was not recorded")

    def _transition(
        self, function: str, lease: FullDwellTimelineLeaseV0_1, *extra: object
    ) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                f"SELECT public.{function}(%s,%s,%s{',%s' * len(extra)}) AS changed",
                (lease.work_id, lease.lease_token, lease.attempt, *extra),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise StaleFullDwellTimelineLeaseError("timeline work lease is stale")


class PostgresFullDwellRefinementDispatchV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def dispatch(self, request: FullDwellRefinementRequestV0_1) -> None:
        from leo_flow.contracts.core import canonical_json_bytes

        payload = json.loads(canonical_json_bytes(request))
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                "SELECT public.dispatch_full_dwell_refinement_v0_1(%s) AS dispatched",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["dispatched"] is not True:
            raise RuntimeError("refinement dispatch was not acknowledged")


def _publication(
    projection: FullDwellTimelineCatalogProjectionV0_1,
    bundle: ObjectRef,
    recording: RecordingObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "analysis_id": projection.analysis_id,
        "recording_id": str(projection.recording_id),
        "recording_identity_digest_value": projection.recording_identity_digest.value,
        "request_digest_value": projection.request_digest.value,
        "data_digest_value": recording.data_object.digest.value,
        "metadata_digest_value": recording.metadata_object.digest.value,
        "manifest_digest_value": recording.manifest_digest.value,
        "bundle_digest_algorithm": bundle.digest.algorithm.value,
        "bundle_digest_value": bundle.digest.value,
        "stream_count": projection.stream_count,
        "window_count": projection.window_count,
        "covered_sample_count": projection.covered_sample_count,
        "idempotency_key": idempotency_key,
    }


def _recording_ref_json(ref: RecordingObjectRef) -> dict[str, object]:
    def item(value: ObjectRef) -> dict[str, object]:
        return {
            "digest": value.digest.value,
            "byte_count": value.byte_count,
            "media_type": value.media_type,
            "format_id": value.format_id,
            "locator": value.locator,
        }

    return {
        "recording_id": str(ref.recording_id),
        "data": item(ref.data_object),
        "metadata": item(ref.metadata_object),
        "manifest_digest": ref.manifest_digest.value,
    }


def _lease(row: dict[str, object]) -> FullDwellTimelineLeaseV0_1:
    request = _mapping(row["request_json"])
    recording = _recording_ref(_mapping(request["recording_ref"]))
    plan_item = _mapping(request["plan"])
    streams = tuple(_stream(_mapping(item)) for item in _array(request["streams"]))
    return FullDwellTimelineLeaseV0_1(
        str(row["work_id"]),
        str(row["lease_token"]),
        _integer(row["lease_generation"]),
        recording,
        FullDwellTimelinePlanV0_1(
            _integer(plan_item["tile_sample_count"]),
            _integer(plan_item["maximum_window_count_per_stream"]),
            _integer(plan_item["maximum_refinements_per_stream"]),
        ),
        streams,
    )


def _stream(item: dict[str, object]) -> FullDwellTimelineStreamSelectionV0_1:
    return FullDwellTimelineStreamSelectionV0_1(
        RadioId(_string(item["radio_id"])),
        _string(item["lnb_id"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        _number(item["sample_rate_hz"]),
        _integer(item["segment_sample_count"]),
    )


def _recording_ref(item: dict[str, object]) -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId(_string(item["recording_id"])),
        _object(_mapping(item["data"])),
        _object(_mapping(item["metadata"])),
        Digest(DigestAlgorithm.SHA256, _string(item["manifest_digest"])),
    )


def _object(item: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        Digest(DigestAlgorithm.SHA256, _string(item["digest"])),
        _integer(item["byte_count"]),
        _string(item["media_type"]),
        _string(item["format_id"]),
        _string(item["locator"]),
    )


def _cataloged(row: dict[str, object]) -> CatalogedFullDwellTimelineV0_1:
    projection = FullDwellTimelineCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        Digest(DigestAlgorithm.SHA256, str(row["recording_identity_digest_value"])),
        Digest(DigestAlgorithm.SHA256, str(row["request_digest_value"])),
        _integer(row["stream_count"]),
        _integer(row["window_count"]),
        _integer(row["covered_sample_count"]),
    )
    return CatalogedFullDwellTimelineV0_1(projection, _blob(row))


def _blob(row: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row["bundle_digest_algorithm"])),
            str(row["bundle_digest_value"]),
        ),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )


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
        "SELECT byte_count,media_type,format_id,locator FROM public.object_blob WHERE digest_algorithm=%s AND digest_value=%s AND lifecycle_state='live'",
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
        raise FullDwellTimelineConflictError("timeline object metadata conflicts")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise TypeError("database timeline request object is invalid")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("database timeline request array is invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("database timeline string is invalid")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database timeline integer is invalid")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("database timeline number is invalid")
    return float(value)
