from __future__ import annotations

import time
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.model_analysis_postgres import (
    AtomicPostgresModelAnalysisCommitter,
)
from leo_flow.adapters.model_postgres_catalog import ModelDatasetMismatchError
from leo_flow.analysis.dataset import DatasetSnapshotRef
from leo_flow.contracts.core import Digest, JobId
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.model_analysis import (
    PreparedModelAnalysis,
    model_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.postgres.test_model_snapshots import _fixture, _seed_authoritative_dataset


def _claimed(postgres_dsn: str, *, ttl_s: float = 5.0):
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    durable_ref = DatasetSnapshotRef(
        snapshot.snapshot_id,
        snapshot.membership_digest,
        Digest.sha256(b"model-dataset-snapshot"),
    )
    jobs = PostgresJobLeaseRepository(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    jobs.enqueue(
        JobId("job_atomic_model"),
        JobType.MODEL_ANALYSIS,
        model_analysis_payload(model_request, durable_ref),
    )
    lease = jobs.claim((JobType.MODEL_ANALYSIS,), "model-worker", ttl_s)
    assert lease is not None
    return jobs, lease, PreparedModelAnalysis(model_request, durable_ref, bundle)


def _committer(postgres_dsn: str, root, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return AtomicPostgresModelAnalysisCommitter(
        FileSystemBlobStore(root),
        connect,
    )


@pytest.mark.integration
def test_model_visibility_and_job_completion_share_one_transaction(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    result = _committer(postgres_dsn, tmp_path / "cas", role=True).commit(
        lease, prepared
    )

    with psycopg.connect(postgres_dsn) as connection:
        model = connection.execute(
            "SELECT model_snapshot_id FROM model_snapshot"
        ).fetchone()
        job = connection.execute(
            "SELECT state, result_ref FROM job WHERE job_id = %s",
            (str(lease.job_id),),
        ).fetchone()
    assert model == (str(prepared.bundle.model_snapshot_id),)
    assert job[0] == "succeeded"
    assert job[1]["artifact_id"] == str(prepared.bundle.model_snapshot_id)
    assert job[1]["digest_value"] == result.digest.value


@pytest.mark.integration
def test_stale_generation_cannot_publish_model_or_complete(
    postgres_dsn: str, tmp_path
) -> None:
    jobs, stale, prepared = _claimed(postgres_dsn, ttl_s=0.03)
    time.sleep(0.05)
    current = jobs.claim((JobType.MODEL_ANALYSIS,), "replacement", 5.0)
    assert current is not None

    with pytest.raises(StaleLeaseError):
        _committer(postgres_dsn, tmp_path / "cas").commit(stale, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT state, lease_generation FROM job WHERE job_id = %s",
            (str(stale.job_id),),
        ).fetchone() == ("leased", current.lease_generation)
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


@pytest.mark.integration
def test_completion_fault_rolls_back_model_and_object_registration(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION reject_atomic_model_completion() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.state = 'succeeded' THEN
                    RAISE EXCEPTION 'injected model completion failure';
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_atomic_model_completion
            BEFORE UPDATE ON job FOR EACH ROW
            EXECUTE FUNCTION reject_atomic_model_completion()
            """
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected"):
            _committer(postgres_dsn, tmp_path / "cas").commit(lease, prepared)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM model_snapshot"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM object_blob "
                "WHERE format_id = 'model-snapshot-bundle-v0.1'"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
            ).fetchone() == ("leased",)
        assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("DROP TRIGGER reject_atomic_model_completion ON job")
            connection.execute("DROP FUNCTION reject_atomic_model_completion()")

    _committer(postgres_dsn, tmp_path / "cas").commit(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
        ).fetchone() == ("succeeded",)


@pytest.mark.integration
def test_substituted_dataset_member_provenance_publishes_nothing(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    substituted = replace(
        prepared.bundle,
        provenance=replace(
            prepared.bundle.provenance,
            input_digests=(
                prepared.bundle.dataset_membership_digest,
                Digest.sha256(b"substituted-feature"),
                prepared.bundle.provenance.input_digests[-1],
            ),
        ),
    )

    with pytest.raises(ModelDatasetMismatchError, match="exact dataset members"):
        _committer(postgres_dsn, tmp_path / "cas").commit(
            lease, replace(prepared, bundle=substituted)
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
        ).fetchone() == ("leased",)
