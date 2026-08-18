# ruff: noqa: F401,F811 -- imported fixture registers disposable PostgreSQL.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from leo_flow.adapters.starlink_full_dwell_work_postgres import (
    PostgresFullDwellWorkRepositoryV0_1,
)
from leo_flow.adapters.starlink_suite_postgres import PostgresStarlinkSuiteCatalogV0_2
from leo_flow.analysis.recording.starlink_suite_persistence import (
    StarlinkSuiteCatalogProjectionV0_2,
)
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.starlink_full_dwell_producer import (
    StaleFullDwellWorkLeaseError,
)
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.adapters.test_starlink_pilot_constellation_postgres_integration import (
    _connect,
    constellation_postgres_dsn,
)


def _object(label: bytes, locator: str) -> ObjectRef:
    return ObjectRef(Digest.sha256(label), len(label), "application/json", "test", locator)


def _source(dsn: str, suffix: str) -> str:
    recording = RecordingObjectRef(
        RecordingId(f"rec_fd_work_{suffix}"),
        _object(f"data-{suffix}".encode(), f"cas:data-{suffix}"),
        _object(f"meta-{suffix}".encode(), f"cas:meta-{suffix}"),
        Digest.sha256(f"manifest-{suffix}".encode()),
    )
    PostgresRecordingCatalog(_connect(dsn, "leo_capture")).publish(
        recording, idempotency_key=f"fd-work:recording:{suffix}"
    )
    analysis_id = "slsuite_" + suffix * 32
    PostgresStarlinkSuiteCatalogV0_2(_connect(dsn, "leo_analysis")).publish_starlink_suite(
        StarlinkSuiteCatalogProjectionV0_2(
            analysis_id,
            str(recording.recording_id),
            recording.identity_digest(),
            Digest.sha256(f"request-{suffix}".encode()),
            "candidates",
            1,
            8,
        ),
        _object(f"suite-{suffix}".encode(), f"cas:suite-{suffix}"),
        recording,
        idempotency_key=f"fd-work:suite:{suffix}",
    )
    return analysis_id


def test_pg16_admission_is_bounded_and_leases_are_fenced(
    constellation_postgres_dsn: str,
) -> None:
    _source(constellation_postgres_dsn, "a")
    _source(constellation_postgres_dsn, "b")
    repository = PostgresFullDwellWorkRepositoryV0_1(
        _connect(constellation_postgres_dsn, "leo_analysis"),
        token_factory=lambda: "fixed",
    )
    admitted = repository.admit(maximum_new=1, maximum_active=1)
    assert (admitted.admitted, admitted.active_backlog, admitted.saturated) == (1, 1, True)
    assert repository.admit(maximum_new=1, maximum_active=1).admitted == 0

    lease = repository.claim("worker", 60)
    assert lease is not None and lease.attempt == lease.lease_generation == 1
    repository.retry(
        lease,
        "retry-test",
        UtcNs(round(datetime.now(UTC).timestamp() * 1_000_000_000) + 60_000_000_000),
    )
    reclaimed = repository.claim("worker", 60)
    assert reclaimed is None  # retry is not available until its explicit timestamp
    with pytest.raises(StaleFullDwellWorkLeaseError):
        repository.park(lease, "stale-test")


def test_pg16_invalid_completion_cannot_close_work(
    constellation_postgres_dsn: str,
) -> None:
    _source(constellation_postgres_dsn, "c")
    repository = PostgresFullDwellWorkRepositoryV0_1(
        _connect(constellation_postgres_dsn, "leo_analysis")
    )
    repository.admit(maximum_new=1, maximum_active=8)
    lease = repository.claim("worker", 60)
    assert lease is not None
    with pytest.raises(StaleFullDwellWorkLeaseError):
        repository.complete(
            lease,
            ArtifactRef("slfd_" + "9" * 32, Digest.sha256(b"missing"), SchemaRef("fd")),
        )
