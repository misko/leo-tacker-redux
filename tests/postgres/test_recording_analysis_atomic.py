from __future__ import annotations

import time

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.recording_analysis_postgres import (
    AtomicPostgresRecordingAnalysisCommitter,
)
from leo_flow.contracts.core import JobId
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.recording_analysis import (
    PreparedRecordingAnalysis,
    recording_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.postgres.test_feature_sets import _publish_recording


def _claimed(postgres_dsn: str, *, ttl_s: float = 5.0):
    request, bundle = _publish_recording(postgres_dsn)
    jobs = PostgresJobLeaseRepository(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    jobs.enqueue(
        JobId("job_atomic_feature"),
        JobType.RECORDING_ANALYSIS,
        recording_analysis_payload(request),
    )
    lease = jobs.claim((JobType.RECORDING_ANALYSIS,), "worker", ttl_s)
    assert lease is not None
    return jobs, lease, PreparedRecordingAnalysis(request, bundle)


def _committer(postgres_dsn: str, root, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return AtomicPostgresRecordingAnalysisCommitter(FileSystemBlobStore(root), connect)


@pytest.mark.integration
def test_feature_visibility_and_job_completion_share_one_transaction(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    result = _committer(postgres_dsn, tmp_path / "cas").commit(lease, prepared)

    with psycopg.connect(postgres_dsn) as connection:
        feature = connection.execute(
            "SELECT feature_set_id FROM feature_set"
        ).fetchone()
        job = connection.execute(
            "SELECT state, result_ref FROM job WHERE job_id = %s",
            (str(lease.job_id),),
        ).fetchone()
    assert feature == (str(prepared.bundle.feature_set_id),)
    assert job[0] == "succeeded"
    assert job[1]["artifact_id"] == str(prepared.bundle.feature_set_id)
    assert job[1]["digest_value"] == result.digest.value


@pytest.mark.integration
def test_stale_generation_cannot_publish_or_complete(
    postgres_dsn: str, tmp_path
) -> None:
    jobs, stale, prepared = _claimed(postgres_dsn, ttl_s=0.03)
    time.sleep(0.05)
    current = jobs.claim((JobType.RECORDING_ANALYSIS,), "replacement", 5.0)
    assert current is not None

    with pytest.raises(StaleLeaseError):
        _committer(postgres_dsn, tmp_path / "cas").commit(stale, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (0,)
        assert connection.execute(
            "SELECT state, lease_generation FROM job WHERE job_id = %s",
            (str(stale.job_id),),
        ).fetchone() == ("leased", current.lease_generation)


@pytest.mark.integration
def test_completion_fault_rolls_back_feature_and_object_registration(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION reject_atomic_completion() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.state = 'succeeded' THEN
                    RAISE EXCEPTION 'injected completion failure';
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_atomic_completion
            BEFORE UPDATE ON job FOR EACH ROW
            EXECUTE FUNCTION reject_atomic_completion()
            """
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected"):
            _committer(postgres_dsn, tmp_path / "cas").commit(lease, prepared)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM feature_set"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM object_blob "
                "WHERE format_id = 'feature-set-bundle-v0.1'"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
            ).fetchone() == ("leased",)
        assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("DROP TRIGGER reject_atomic_completion ON job")
            connection.execute("DROP FUNCTION reject_atomic_completion()")


@pytest.mark.integration
def test_atomic_committer_operates_as_analysis_role(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    _committer(postgres_dsn, tmp_path / "cas", role=True).commit(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM job WHERE state = 'succeeded'"
        ).fetchone() == (1,)
