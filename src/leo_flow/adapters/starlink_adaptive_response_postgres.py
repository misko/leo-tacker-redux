"""PostgreSQL catalog and fenced work adapter for adaptive pilot responses."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
    CatalogedStarlinkAdaptiveResponseV0_1,
    StarlinkAdaptiveResponseCatalogV0_1,
    StarlinkAdaptiveResponseConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseCatalogProjectionV0_1,
    StarlinkAdaptiveResponseProductRefV0_1,
)
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1 as FULL_DWELL_TIMELINE_V0_1,
)
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellRefinementRequestV0_1,
    FullDwellRefinementWindowV0_1,
    FullDwellTimelineBundleV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.starlink_adaptive_response import (
    AdaptiveResponseWorkLeaseV0_1,
    StaleAdaptiveResponseLeaseError,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkAdaptiveResponseCatalogV0_1(StarlinkAdaptiveResponseCatalogV0_1):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_adaptive_response(
        self,
        projection: StarlinkAdaptiveResponseCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkAdaptiveResponseProductRefV0_1:
        payload = {
            "analysis_id": projection.analysis_id,
            "recording_id": str(projection.recording_id),
            "input_recording_digest_value": projection.recording_identity_digest.value,
            "timeline_analysis_id": projection.timeline_ref.artifact_id,
            "timeline_bundle_digest_value": projection.timeline_ref.digest.value,
            "source_suite_analysis_id": projection.source_suite_ref.artifact_id,
            "source_suite_bundle_digest_value": projection.source_suite_ref.digest.value,
            "request_digest_value": projection.request_digest.value,
            "bundle_digest_value": bundle_ref.digest.value,
            "stream_count": projection.stream_count,
            "window_count": projection.window_count,
            "point_count": projection.point_count,
            "idempotency_key": idempotency_key,
        }
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise StarlinkAdaptiveResponseConflictError(
                "adaptive response recording closure differs"
            )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_starlink_adaptive_response_v0_1(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise StarlinkAdaptiveResponseConflictError(
                "adaptive response publication was not acknowledged"
            )
        return StarlinkAdaptiveResponseProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_adaptive_response(
        self, ref: StarlinkAdaptiveResponseProductRefV0_1
    ) -> CatalogedStarlinkAdaptiveResponseV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                "SELECT * FROM public.read_exact_recording_starlink_adaptive_response_v0_1(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("adaptive response exact read is ambiguous")
        return _cataloged(rows[0])

    def latest_starlink_adaptive_response(
        self, recording_id: RecordingId
    ) -> StarlinkAdaptiveResponseProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            rows = cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_adaptive_response_v0_1(%s)",
                (str(recording_id),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("adaptive response latest read is ambiguous")
        return _cataloged(rows[0]).ref


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
        raise StarlinkAdaptiveResponseConflictError(
            "adaptive response object metadata conflicts"
        )


class PostgresAdaptiveResponseWorkRepositoryV0_1:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: uuid.uuid4().hex)

    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> AdaptiveResponseWorkLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("adaptive response claim bounds are invalid")
        token = f"{worker_id}:slarlease_{self._token()}"
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            rows = cursor.execute(
                "SELECT * FROM public.claim_starlink_adaptive_response_work_v0_1(%s,%s)",
                (token, timedelta(seconds=lease_ttl_s)),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("adaptive response claim is ambiguous")
        row = rows[0]
        return AdaptiveResponseWorkLeaseV0_1(
            _refinement_request(_mapping(row["request_json"])),
            StarlinkDetectorSuiteProductRefV0_2(
                str(row["source_suite_analysis_id"]),
                RecordingId(str(row["recording_id"])),
                _blob(row),
            ),
            Digest(
                DigestAlgorithm.SHA256,
                str(row["source_suite_request_digest_value"]),
            ),
            str(row["lease_token"]),
            _integer(row["lease_generation"]),
            _integer(row["attempt"]),
        )

    def complete(
        self, lease: AdaptiveResponseWorkLeaseV0_1, result: ArtifactRef
    ) -> None:
        self._transition(
            "complete_starlink_adaptive_response_work_v0_1",
            lease,
            result.artifact_id,
            result.digest.value,
        )

    def retry(self, lease: AdaptiveResponseWorkLeaseV0_1, reason: str) -> None:
        self._transition("retry_starlink_adaptive_response_work_v0_1", lease, reason)

    def _transition(
        self,
        function: str,
        lease: AdaptiveResponseWorkLeaseV0_1,
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
                    lease.refinement_request.timeline_ref.artifact_id,
                    lease.lease_token,
                    lease.lease_generation,
                    *extra,
                ),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise StaleAdaptiveResponseLeaseError(
                "adaptive response work lease is stale"
            )


def _refinement_request(item: dict[str, object]) -> FullDwellRefinementRequestV0_1:
    return FullDwellRefinementRequestV0_1(
        _schema(_mapping(item["schema"])),
        RecordingId(_string(item["recording_id"])),
        _recording_ref(_mapping(item["recording_object_ref"])),
        _artifact(_mapping(item["timeline_ref"])),
        _digest(_mapping(item["timeline_request_digest"])),
        tuple(_refinement_window(_mapping(entry)) for entry in _array(item["windows"])),
        _string(item["selection_policy"]),
        _boolean(item["candidate_only"]),
    )


def _refinement_window(item: dict[str, object]) -> FullDwellRefinementWindowV0_1:
    return FullDwellRefinementWindowV0_1(
        RadioId(_string(item["radio_id"])),
        _string(item["lnb_id"]),
        SegmentId(_string(item["segment_id"])),
        ReceiverChainId(_string(item["receiver_chain_id"])),
        _integer(item["channel_number"]),
        StarlinkEdge(_string(item["edge"])),
        _integer(item["rank"]),
        _integer(item["start_sample"]),
        _integer(item["stop_sample"]),
    )


def _recording_ref(item: dict[str, object]) -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId(_string(item["recording_id"])),
        _object(_mapping(item["data_object"])),
        _object(_mapping(item["metadata_object"])),
        _digest(_mapping(item["manifest_digest"])),
    )


def _object(item: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        _digest(_mapping(item["digest"])),
        _integer(item["byte_count"]),
        _string(item["media_type"]),
        _string(item["format_id"]),
        _string(item["locator"]),
    )


def _artifact(item: dict[str, object]) -> ArtifactRef:
    schema = item["schema"]
    return ArtifactRef(
        _string(item["artifact_id"]),
        _digest(_mapping(item["digest"])),
        None if schema is None else _schema(_mapping(schema)),
    )


def _schema(item: dict[str, object]) -> SchemaRef:
    version = _mapping(item["version"])
    return SchemaRef(
        _string(item["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _digest(item: dict[str, object]) -> Digest:
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _cataloged(
    row: dict[str, object],
) -> CatalogedStarlinkAdaptiveResponseV0_1:
    return CatalogedStarlinkAdaptiveResponseV0_1(
        StarlinkAdaptiveResponseCatalogProjectionV0_1(
            str(row["analysis_id"]),
            RecordingId(str(row["recording_id"])),
            Digest(DigestAlgorithm.SHA256, str(row["input_recording_digest_value"])),
            ArtifactRef(
                str(row["timeline_analysis_id"]),
                Digest(
                    DigestAlgorithm.SHA256,
                    str(row["timeline_bundle_digest_value"]),
                ),
                SchemaRef(
                    FullDwellTimelineBundleV0_1.SCHEMA_ID,
                    FULL_DWELL_TIMELINE_V0_1,
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
        ),
        _blob(row),
    )


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


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected database JSON object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected database JSON array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected database JSON string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return int(str(value))
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected database JSON boolean")
    return value
