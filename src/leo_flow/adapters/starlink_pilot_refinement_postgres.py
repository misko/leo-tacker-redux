"""PostgreSQL catalog and fenced work adapter for exact pilot refinement."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.recording.starlink_pilot_refinement_persistence import (
    CatalogedStarlinkPilotRefinementV0_1,
    StarlinkPilotRefinementCatalogV0_1,
    StarlinkPilotRefinementConflictError,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenBundleV0_1,
    StarlinkPilotPrescreenProductRefV0_1,
)
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementCatalogProjectionV0_1,
    StarlinkPilotRefinementProductRefV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.starlink_pilot_refinement import (
    PilotRefinementWorkLeaseV0_1,
    StalePilotRefinementLeaseError,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresStarlinkPilotRefinementCatalogV0_1(StarlinkPilotRefinementCatalogV0_1):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish_starlink_pilot_refinement(
        self,
        projection: StarlinkPilotRefinementCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotRefinementProductRefV0_1:
        if recording_ref.identity_digest() != projection.recording_identity_digest:
            raise StarlinkPilotRefinementConflictError(
                "pilot-refinement recording closure differs"
            )
        payload = {
            "analysis_id": projection.analysis_id,
            "recording_id": str(projection.recording_id),
            "recording_identity_digest_value": projection.recording_identity_digest.value,
            "source_prescreen_analysis_id": projection.source_prescreen_ref.artifact_id,
            "source_prescreen_bundle_digest_value": projection.source_prescreen_ref.digest.value,
            "source_suite_analysis_id": projection.source_suite_ref.artifact_id,
            "source_suite_bundle_digest_value": projection.source_suite_ref.digest.value,
            "request_digest_value": projection.request_digest.value,
            "bundle_digest_value": bundle_ref.digest.value,
            "stream_count": projection.stream_count,
            "seed_count": projection.seed_count,
            "point_count": projection.point_count,
            "idempotency_key": idempotency_key,
        }
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_live_object(cursor, bundle_ref)
            row = cursor.execute(
                "SELECT public.publish_recording_starlink_pilot_refinement_v0_1(%s) AS published",
                (Jsonb(payload),),
            ).fetchone()
        if row is None or row["published"] is not True:
            raise StarlinkPilotRefinementConflictError(
                "pilot-refinement publication was not acknowledged"
            )
        return StarlinkPilotRefinementProductRefV0_1(
            projection.analysis_id, projection.recording_id, bundle_ref
        )

    def get_starlink_pilot_refinement(
        self, ref: StarlinkPilotRefinementProductRefV0_1
    ) -> CatalogedStarlinkPilotRefinementV0_1 | None:
        rows = self._read(
            "read_exact_recording_starlink_pilot_refinement_v0_1",
            ref.analysis_id,
            str(ref.recording_id),
            ref.bundle_ref.digest.algorithm.value,
            ref.bundle_ref.digest.value,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("pilot-refinement exact read is ambiguous")
        return _cataloged(rows[0])

    def latest_starlink_pilot_refinement(
        self, recording_id: RecordingId
    ) -> StarlinkPilotRefinementProductRefV0_1 | None:
        rows = self._read(
            "read_latest_recording_starlink_pilot_refinement_v0_1", str(recording_id)
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("pilot-refinement latest read is ambiguous")
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


class PostgresPilotRefinementWorkRepositoryV0_1:
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
    ) -> PilotRefinementWorkLeaseV0_1 | None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("pilot-refinement claim bounds are invalid")
        token = f"{worker_id}:slprlease_{self._token()}"
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            rows = cursor.execute(
                "SELECT * FROM public.claim_starlink_pilot_refinement_work_v0_1(%s,%s)",
                (token, timedelta(seconds=lease_ttl_s)),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("pilot-refinement claim is ambiguous")
        row = rows[0]
        recording_id = RecordingId(str(row["recording_id"]))
        return PilotRefinementWorkLeaseV0_1(
            str(row["source_prescreen_analysis_id"]),
            _recording_ref(row),
            StarlinkPilotPrescreenProductRefV0_1(
                str(row["source_prescreen_analysis_id"]),
                recording_id,
                _blob(row, "prescreen_bundle"),
            ),
            StarlinkDetectorSuiteProductRefV0_2(
                str(row["source_suite_analysis_id"]),
                recording_id,
                _blob(row, "suite_bundle"),
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
        self, lease: PilotRefinementWorkLeaseV0_1, result: ArtifactRef
    ) -> None:
        self._transition(
            "complete_starlink_pilot_refinement_work_v0_1",
            lease,
            result.artifact_id,
            result.digest.value,
        )

    def retry(self, lease: PilotRefinementWorkLeaseV0_1, reason: str) -> None:
        self._transition("retry_starlink_pilot_refinement_work_v0_1", lease, reason)

    def _transition(
        self,
        function: str,
        lease: PilotRefinementWorkLeaseV0_1,
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
                    lease.source_prescreen_ref.analysis_id,
                    lease.lease_token,
                    lease.lease_generation,
                    *extra,
                ),
            ).fetchone()
        if row is None or row["changed"] is not True:
            raise StalePilotRefinementLeaseError("pilot-refinement work lease is stale")


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
        raise StarlinkPilotRefinementConflictError(
            "pilot-refinement object metadata conflicts"
        )


def _recording_ref(row: dict[str, object]) -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId(str(row["recording_id"])),
        _blob(row, "data"),
        _blob(row, "metadata"),
        Digest(DigestAlgorithm.SHA256, str(row["recording_manifest_digest_value"])),
    )


def _cataloged(row: dict[str, object]) -> CatalogedStarlinkPilotRefinementV0_1:
    return CatalogedStarlinkPilotRefinementV0_1(
        StarlinkPilotRefinementCatalogProjectionV0_1(
            str(row["analysis_id"]),
            RecordingId(str(row["recording_id"])),
            Digest(
                DigestAlgorithm.SHA256,
                str(row["recording_identity_digest_value"]),
            ),
            ArtifactRef(
                str(row["source_prescreen_analysis_id"]),
                Digest(
                    DigestAlgorithm.SHA256,
                    str(row["source_prescreen_bundle_digest_value"]),
                ),
                SchemaRef(StarlinkPilotPrescreenBundleV0_1.SCHEMA_ID, V0_1),
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
            _integer(row["seed_count"]),
            _integer(row["point_count"]),
        ),
        _blob(row, "bundle"),
    )


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
        return int(str(value))
    return value
