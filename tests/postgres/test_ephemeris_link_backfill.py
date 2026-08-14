from __future__ import annotations

import time

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.ephemeris_link_postgres import (
    AtomicPostgresEphemerisLinkCommitter,
)
from leo_flow.analysis.ephemeris.backfill import (
    EphemerisLinkRequest,
    PreparedEphemerisLink,
    ephemeris_link_payload,
)
from leo_flow.analysis.ephemeris.postgres_catalog import (
    PostgresEphemerisSnapshotCatalog,
    connection_factory,
)
from leo_flow.contracts.core import ArtifactRef, JobId, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from testkit import digest
from tests.postgres.test_ephemeris import archived
from tests.recording_analysis.test_feature_persistence import _fixture

START = 1_721_177_100_000_000_000
FINISH = 1_721_177_200_000_000_000


def _committer(postgres_dsn: str, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return AtomicPostgresEphemerisLinkCommitter(connect)


def _claimed(postgres_dsn: str, *, ttl_s: float = 5.0):
    recording_request, _ = _fixture()
    ref = recording_request.recording_object_ref
    PostgresRecordingCatalog(connection_factory(postgres_dsn)).publish(
        ref, idempotency_key="ephemeris-link-recording"
    )
    catalog = PostgresEphemerisSnapshotCatalog(connection_factory(postgres_dsn))
    catalog.publish(archived("before", retrieved_at=START - 10))
    catalog.publish(archived("after", retrieved_at=FINISH + 10))
    request = EphemerisLinkRequest(
        ref.recording_id,
        EphemerisSource.HUGGING_FACE,
        "starlink",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        ArtifactRef("temporal-v1", digest("temporal")),
        UtcNs(FINISH + 100),
    )
    jobs = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    jobs.enqueue(
        JobId("job_ephemeris_link"),
        JobType.EPHEMERIS_LINK_BACKFILL,
        ephemeris_link_payload(request),
    )
    lease = jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "worker", ttl_s)
    assert lease is not None
    return (
        jobs,
        lease,
        PreparedEphemerisLink(
            request, ref, RecordingInterval(UtcNs(START), UtcNs(FINISH))
        ),
    )


@pytest.mark.integration
def test_temporal_link_and_completion_are_atomic_and_exact(
    postgres_dsn: str,
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    result = _committer(postgres_dsn, role=True).commit(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        link = connection.execute(
            "SELECT snapshot_id, selection_policy, as_of_utc_ns "
            "FROM recording_ephemeris_link"
        ).fetchone()
        job = connection.execute(
            "SELECT state, result_ref FROM job WHERE job_id = %s",
            (str(lease.job_id),),
        ).fetchone()
    assert link == ("eph_before", "available_then", prepared.request.as_of_utc_ns)
    assert job[0] == "succeeded"
    assert job[1]["artifact_id"] == result.artifact_id


@pytest.mark.integration
def test_stale_link_lease_publishes_nothing(postgres_dsn: str) -> None:
    jobs, stale, prepared = _claimed(postgres_dsn, ttl_s=0.03)
    time.sleep(0.05)
    current = jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "replacement", 5.0)
    assert current is not None
    with pytest.raises(StaleLeaseError):
        _committer(postgres_dsn, role=True).commit(stale, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording_ephemeris_link"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = %s", (str(stale.job_id),)
        ).fetchone() == ("leased",)


@pytest.mark.integration
def test_completion_fault_rolls_back_link(postgres_dsn: str) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION reject_link_completion() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.state = 'succeeded' THEN RAISE EXCEPTION 'link completion fault'; END IF;
              RETURN NEW;
            END $$
            """
        )
        connection.execute(
            "CREATE TRIGGER reject_link_completion BEFORE UPDATE ON job "
            "FOR EACH ROW EXECUTE FUNCTION reject_link_completion()"
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="link completion"):
            _committer(postgres_dsn, role=True).commit(lease, prepared)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM recording_ephemeris_link"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
            ).fetchone() == ("leased",)
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("DROP TRIGGER reject_link_completion ON job")
            connection.execute("DROP FUNCTION reject_link_completion()")

    # A retry of the same fenced job is content-idempotent after rollback.
    _committer(postgres_dsn, role=True).commit(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording_ephemeris_link"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
        ).fetchone() == ("succeeded",)
