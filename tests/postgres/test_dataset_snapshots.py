from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.analysis.dataset import (
    DatasetSnapshotIntegrityError,
    DatasetSnapshotNotFoundError,
    DurableDatasetSnapshotRepository,
)
from leo_flow.analysis.dataset.postgres_catalog import (
    DatasetSnapshotConflictError,
    PostgresDatasetSnapshotCatalog,
    connection_factory,
)
from leo_flow.contracts.core import Digest
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.dataset_analysis.test_dataset_snapshot import _snapshot


def _repository(postgres_dsn: str, root):
    return DurableDatasetSnapshotRepository(
        FileSystemBlobStore(root),
        PostgresDatasetSnapshotCatalog(connection_factory(postgres_dsn)),
    )


class _ReadOnlyAuditConnection:
    def __init__(self, connection, observations: list[str]) -> None:
        self._connection = connection
        self._observations = observations

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        state = self._connection.execute("SHOW transaction_read_only").fetchone()
        assert state is not None
        self._observations.append(str(state["transaction_read_only"]))
        return self._connection.__exit__(*args)

    def cursor(self, *args, **kwargs):
        return self._connection.cursor(*args, **kwargs)


@pytest.mark.integration
def test_snapshot_and_all_members_become_visible_atomically(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()

    assert (
        repository.publish(snapshot, idempotency_key="dataset:atomic") == snapshot.ref
    )
    assert repository.get(snapshot.ref) == snapshot
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM dataset_snapshot"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM dataset_member").fetchone() == (
            len(snapshot.members),
        )


@pytest.mark.integration
def test_exact_retry_is_idempotent_but_key_reuse_conflicts(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()
    first = repository.publish(snapshot, idempotency_key="dataset:stable")
    assert repository.publish(snapshot, idempotency_key="dataset:stable") == first

    with pytest.raises(DatasetSnapshotConflictError):
        repository.publish(snapshot, idempotency_key="dataset:different-key")

    other = _snapshot(context=False)
    restarted_repository = _repository(postgres_dsn, tmp_path / "cas")
    with pytest.raises(DatasetSnapshotConflictError, match="idempotency"):
        restarted_repository.publish(other, idempotency_key="dataset:stable")


@pytest.mark.integration
def test_reader_requires_exact_dual_digest_ref(postgres_dsn: str, tmp_path) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()
    repository.publish(snapshot, idempotency_key="dataset:exact-ref")
    substituted = type(snapshot.ref)(
        snapshot.ref.snapshot_id,
        snapshot.ref.feature_membership_digest,
        Digest.sha256(b"substituted"),
    )

    with pytest.raises(DatasetSnapshotNotFoundError):
        repository.get(substituted)


@pytest.mark.integration
def test_catalog_reader_transaction_is_read_only(postgres_dsn: str, tmp_path) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()
    repository.publish(snapshot, idempotency_key="dataset:read-only")
    observations: list[str] = []

    def audited_connection():
        return _ReadOnlyAuditConnection(
            psycopg.connect(postgres_dsn, row_factory=dict_row), observations
        )

    reader = DurableDatasetSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresDatasetSnapshotCatalog(audited_connection),  # type: ignore[arg-type]
    )

    assert reader.get(snapshot.ref) == snapshot
    assert observations == ["on"]


@pytest.mark.integration
def test_reader_rejects_projection_tampering(postgres_dsn: str, tmp_path) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()
    repository.publish(snapshot, idempotency_key="dataset:tamper")
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE dataset_member
            SET split_group_id = 'tampered-group'
            WHERE snapshot_id = %s AND member_index = 0
            """,
            (str(snapshot.ref.snapshot_id),),
        )

    with pytest.raises(DatasetSnapshotIntegrityError, match="projection"):
        repository.get(snapshot.ref)


@pytest.mark.integration
def test_member_failure_rolls_back_catalog_and_object_link(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    snapshot = _snapshot()
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION fail_second_dataset_member() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.member_index = 1 THEN
                    RAISE EXCEPTION 'injected member failure';
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER fail_dataset_member
            BEFORE INSERT ON dataset_member
            FOR EACH ROW EXECUTE FUNCTION fail_second_dataset_member()
            """
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected member"):
            repository.publish(snapshot, idempotency_key="dataset:partial")
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM dataset_snapshot"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM dataset_member"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM object_blob"
            ).fetchone() == (0,)
        assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("DROP TRIGGER fail_dataset_member ON dataset_member")
            connection.execute("DROP FUNCTION fail_second_dataset_member()")


@pytest.mark.integration
def test_concurrent_exact_publication_exposes_one_snapshot(
    postgres_dsn: str, tmp_path
) -> None:
    snapshot = _snapshot()

    def publish(_index: int):
        return _repository(postgres_dsn, tmp_path / "cas").publish(
            snapshot, idempotency_key="dataset:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(6)))
    assert all(result == snapshot.ref for result in results)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dataset_snapshot"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM dataset_member").fetchone() == (
            len(snapshot.members),
        )


@pytest.mark.integration
def test_repository_operates_as_actual_analysis_role(
    postgres_dsn: str, tmp_path
) -> None:
    def role_connection():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute("SET ROLE leo_analysis")
        return connection

    repository = DurableDatasetSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresDatasetSnapshotCatalog(role_connection),
    )
    snapshot = _snapshot()

    repository.publish(snapshot, idempotency_key="dataset:analysis-role")
    assert (
        repository.publish(snapshot, idempotency_key="dataset:analysis-role")
        == snapshot.ref
    )
    assert repository.get(snapshot.ref) == snapshot
