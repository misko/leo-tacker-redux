"""PostgreSQL catalog adapter for immutable full-dwell response v0.1."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
    CatalogedStarlinkFullDwellV0_1,
    StarlinkFullDwellConflictError,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellCatalogProjectionV0_1,
    StarlinkFullDwellProductRefV0_1,
    StarlinkFullDwellResponseBundleV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkFullDwellCatalogV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_full_dwell(
        self,
        projection: StarlinkFullDwellCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
        bundle: StarlinkFullDwellResponseBundleV0_1 | None = None,
    ) -> StarlinkFullDwellProductRefV0_1:
        if not idempotency_key or bundle is None:
            raise ValueError("idempotency key and full bundle are required")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            published = PostgresRecordingCatalog.get_with_cursor(
                cursor, str(recording_ref.recording_id)
            )
            if published is None or published.recording_object != recording_ref:
                raise ValueError("full-dwell input is not the exact recording")
            _register_live_object(cursor, bundle_ref)
            header = _header(projection, bundle_ref, idempotency_key)
            try:
                cursor.execute(
                    "SELECT public.publish_recording_starlink_full_dwell_v0_1(%s,%s) AS published",
                    (Jsonb(header), Jsonb(_points(bundle))),
                )
                row = cursor.fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise StarlinkFullDwellConflictError(
                    "full-dwell catalog identity was reused"
                ) from error
            if row is None or row["published"] is not True:
                raise StarlinkFullDwellConflictError(
                    "full-dwell publication was not acknowledged"
                )
        return StarlinkFullDwellProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_full_dwell(
        self, ref: StarlinkFullDwellProductRefV0_1
    ) -> CatalogedStarlinkFullDwellV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_exact_recording_starlink_full_dwell_v0_1(%s,%s,%s,%s)",
                (
                    ref.analysis_id,
                    str(ref.recording_id),
                    ref.bundle_ref.digest.algorithm.value,
                    ref.bundle_ref.digest.value,
                ),
            )
            row = cursor.fetchone()
        result = None if row is None else _cataloged(row)
        return result if result is not None and result.ref == ref else None

    def latest_starlink_full_dwell(
        self, recording_id: RecordingId
    ) -> StarlinkFullDwellProductRefV0_1 | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_latest_recording_starlink_full_dwell_v0_1(%s)",
                (str(recording_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        shell = ObjectRef(
            Digest(
                DigestAlgorithm(str(row["bundle_digest_algorithm"])),
                str(row["bundle_digest_value"]),
            ),
            1,
            "application/octet-stream",
            "unresolved",
            "unresolved",
        )
        # Resolve metadata only through the exact routine; request locators are never trusted.
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_exact_recording_starlink_full_dwell_v0_1(%s,%s,%s,%s)",
                (
                    str(row["analysis_id"]),
                    str(recording_id),
                    shell.digest.algorithm.value,
                    shell.digest.value,
                ),
            )
            exact = cursor.fetchone()
        if exact is None:
            raise RuntimeError("latest full-dwell reference cannot be resolved")
        return _cataloged(exact).ref


def _header(projection, ref, idempotency_key):
    return {
        "analysis_id": projection.analysis_id,
        "recording_id": str(projection.recording_id),
        "input_recording_digest_algorithm": "sha256",
        "input_recording_digest_value": projection.input_recording_digest.value,
        "source_suite_analysis_id": projection.source_suite_ref.artifact_id,
        "source_suite_bundle_digest_algorithm": "sha256",
        "source_suite_bundle_digest_value": projection.source_suite_ref.digest.value,
        "source_suite_request_digest_algorithm": "sha256",
        "source_suite_request_digest_value": projection.source_suite_request_digest.value,
        "request_digest_algorithm": "sha256",
        "request_digest_value": projection.request_digest.value,
        "bundle_digest_algorithm": "sha256",
        "bundle_digest_value": ref.digest.value,
        "stream_count": projection.stream_count,
        "prescreen_window_count": projection.prescreen_window_count,
        "exact_window_count": projection.exact_window_count,
        "point_count": projection.point_count,
        "idempotency_key": idempotency_key,
    }


def _points(bundle):
    rows = []
    for stream in bundle.streams:
        for point in stream.points:
            rows.append(
                {
                    "segment_id": str(point.segment_id),
                    "radio_id": str(point.radio_id),
                    "receiver_chain_id": str(point.receiver_chain_id),
                    "channel_number": stream.channel_number,
                    "edge": point.edge.value,
                    "method": point.method.value,
                    "window_index": point.window_index,
                    "start_sample": point.start_sample,
                    "stop_sample": point.stop_sample,
                    "interval_start_utc_ns": int(point.interval_start_utc_ns),
                    "interval_stop_utc_ns": int(point.interval_stop_utc_ns),
                    "prescreen_score": point.prescreen_score,
                    "qin_score": point.qin.score,
                    "qin_winning_epoch_sample_in_segment": point.qin.winning_epoch_sample_in_segment,
                    "qin_winning_coarse_cfo_hz": point.qin.winning_coarse_cfo_hz,
                    "qin_winning_residual_cfo_hz": point.qin.winning_residual_cfo_hz,
                    "surrogate_scores": [
                        item.winner.score for item in point.surrogates
                    ],
                    "surrogate_winners": [
                        {
                            "codebook_index": item.codebook_index,
                            "template_digest": item.template_digest.value,
                            "winning_epoch_sample_in_segment": item.winner.winning_epoch_sample_in_segment,
                            "winning_coarse_cfo_hz": item.winner.winning_coarse_cfo_hz,
                            "winning_residual_cfo_hz": item.winner.winning_residual_cfo_hz,
                        }
                        for item in point.surrogates
                    ],
                    "finite_upper_tail_rank": point.finite_upper_tail_rank,
                    "qin_minus_max_surrogate": point.qin_minus_max_surrogate,
                    "dependence_group": point.dependence_group,
                }
            )
    return rows


def _register_live_object(cursor, ref):
    values = (
        ref.digest.algorithm.value,
        ref.digest.value,
        ref.byte_count,
        ref.media_type,
        ref.format_id,
        ref.locator,
    )
    cursor.execute("SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)", values)
    cursor.execute(
        "SELECT byte_count,media_type,format_id,locator FROM public.object_blob WHERE digest_algorithm=%s AND digest_value=%s AND lifecycle_state='live'",
        values[:2],
    )
    row = cursor.fetchone()
    if (
        row is None
        or (
            int(row["byte_count"]),
            str(row["media_type"]),
            str(row["format_id"]),
            str(row["locator"]),
        )
        != values[2:]
    ):
        raise StarlinkFullDwellConflictError("full-dwell object metadata conflicts")


def _cataloged(row):
    digest = lambda value: Digest(DigestAlgorithm.SHA256, str(value))
    projection = StarlinkFullDwellCatalogProjectionV0_1(
        str(row["analysis_id"]),
        RecordingId(str(row["recording_id"])),
        digest(row["input_recording_digest_value"]),
        ArtifactRef(
            str(row["source_suite_analysis_id"]),
            digest(row["source_suite_bundle_digest_value"]),
            SchemaRef(
                "org.leo-flow.starlink-detector-suite-recording-bundle",
                SchemaVersion(0, 2),
            ),
        ),
        digest(row["source_suite_request_digest_value"]),
        digest(row["request_digest_value"]),
        int(row["stream_count"]),
        int(row["prescreen_window_count"]),
        int(row["exact_window_count"]),
        int(row["point_count"]),
    )
    ref = ObjectRef(
        digest(row["bundle_digest_value"]),
        int(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedStarlinkFullDwellV0_1(projection, ref)
