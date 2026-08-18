from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.starlink_symbolwise_replay_postgres import (
    PostgresStarlinkSymbolwiseReplayRepositoryV0_1,
)
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
    StarlinkSymbolwiseReplayConflictError,
)
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    StarlinkSymbolwiseReplayCatalogProjectionV0_1,
    StarlinkSymbolwiseReplayPublicationFenceV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.services.starlink_symbolwise_replay_product import (
    StaleStarlinkSymbolwiseReplayLeaseError,
)
from tests.recording_analysis.symbolwise_product_fixtures import (
    recording_ref,
    request,
)


def _connect_as(postgres_dsn: str, role: str):  # type: ignore[no-untyped-def]
    def connect():  # type: ignore[no-untyped-def]
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _object(label: str) -> ObjectRef:
    digest = Digest.sha256(label.encode())
    return ObjectRef(
        digest,
        64,
        "application/json",
        "starlink-symbolwise-recording-v0.1",
        f"cas:sha256:{digest.value}",
    )


def _seed_recording(postgres_dsn: str):  # type: ignore[no-untyped-def]
    recording = recording_ref()
    with psycopg.connect(postgres_dsn) as connection:
        for ref in (recording.data_object, recording.metadata_object):
            connection.execute(
                "SELECT public.register_live_object_blob(%s,%s,%s,%s,%s,%s)",
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    ref.byte_count,
                    ref.media_type,
                    ref.format_id,
                    ref.locator,
                ),
            )
        connection.execute(
            "INSERT INTO public.recording(recording_id,data_digest_value,metadata_digest_value,manifest_digest_value,idempotency_key,state) VALUES(%s,%s,%s,%s,%s,'published')",
            (
                str(recording.recording_id),
                recording.data_object.digest.value,
                recording.metadata_object.digest.value,
                recording.manifest_digest.value,
                "recording:symbolwise-product-pg",
            ),
        )
    return recording


@pytest.mark.integration
def test_explicit_queue_publication_is_fenced_replay_safe_and_dashboard_readable(
    postgres_dsn: str,
) -> None:
    recording = _seed_recording(postgres_dsn)
    replay_request = request(recording_object=recording)
    analysis = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(
        _connect_as(postgres_dsn, "leo_analysis"), token_factory=lambda: "fixed"
    )

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM public.starlink_symbolwise_replay_work_v0_1"
        ).fetchone() == (0,)
    work_id = analysis.enqueue(
        replay_request, priority=80, idempotency_key="symbolwise:explicit:pg"
    )
    assert (
        analysis.enqueue(
            replay_request, priority=80, idempotency_key="symbolwise:explicit:pg"
        )
        == work_id
    )
    lease = analysis.claim("worker-a", 7_200)
    assert lease is not None
    assert lease.recording_ref == recording
    assert lease.request == replay_request
    assert lease.attempt == 1 and lease.lease_generation == 1

    bundle_ref = _object("symbolwise-result")
    projection = StarlinkSymbolwiseReplayCatalogProjectionV0_1(
        "slsymrec_" + "c" * 32,
        recording.recording_id,
        recording.identity_digest(),
        replay_request.digest,
        1,
        600,
        3_000,
        True,
    )
    stale_fence = StarlinkSymbolwiseReplayPublicationFenceV0_1(
        lease.work_id, lease.lease_token, lease.lease_generation + 1
    )
    with pytest.raises(StarlinkSymbolwiseReplayConflictError):
        analysis.publish_starlink_symbolwise_replay(
            projection,
            bundle_ref,
            recording,
            lease_fence=stale_fence,
            idempotency_key="symbolwise:result:pg",
        )
    fence = replace(stale_fence, lease_generation=lease.lease_generation)
    published = analysis.publish_starlink_symbolwise_replay(
        projection,
        bundle_ref,
        recording,
        lease_fence=fence,
        idempotency_key="symbolwise:result:pg",
    )
    assert (
        analysis.publish_starlink_symbolwise_replay(
            projection,
            bundle_ref,
            recording,
            lease_fence=fence,
            idempotency_key="symbolwise:result:pg",
        )
        == published
    )
    analysis.complete(
        lease, ArtifactRef(published.analysis_id, published.bundle_ref.digest, None)
    )
    with pytest.raises(StaleStarlinkSymbolwiseReplayLeaseError):
        analysis.complete(
            lease, ArtifactRef(published.analysis_id, published.bundle_ref.digest, None)
        )
    assert analysis.claim("worker-a", 7_200) is None

    dashboard = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(
        _connect_as(postgres_dsn, "leo_dashboard")
    )
    assert (
        dashboard.latest_starlink_symbolwise_replay(recording.recording_id) == published
    )
    assert dashboard.get_starlink_symbolwise_replay(published).ref == published


@pytest.mark.integration
def test_capture_role_cannot_enqueue_optional_symbolwise_work(
    postgres_dsn: str,
) -> None:
    recording = _seed_recording(postgres_dsn)
    capture = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(
        _connect_as(postgres_dsn, "leo_capture")
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        capture.enqueue(
            request(recording_object=recording),
            priority=0,
            idempotency_key="capture:must-not-enqueue-symbolwise",
        )
