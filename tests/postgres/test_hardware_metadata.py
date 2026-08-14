from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.hardware_postgres_catalog import (
    HardwareSnapshotConflictError,
    PostgresHardwareSnapshotCatalog,
)
from leo_flow.contracts.core import Digest, HardwareSnapshotId
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.hardware import (
    DurableHardwareMetadataRepository,
    HardwareSnapshotIntegrityError,
    HardwareSnapshotNotFoundError,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.hardware.test_hardware_persistence import _snapshot


def _repository(postgres_dsn: str, root, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return DurableHardwareMetadataRepository(
        FileSystemBlobStore(root),
        PostgresHardwareSnapshotCatalog(connect),
    )


@pytest.mark.integration
def test_hardware_publication_is_exact_normalized_and_idempotent(
    postgres_dsn: str, tmp_path
) -> None:
    snapshot = _snapshot()
    repository = _repository(postgres_dsn, tmp_path / "cas")

    ref = repository.publish(snapshot, idempotency_key="hardware:v5")
    assert repository.publish(snapshot, idempotency_key="hardware:v5") == ref
    assert repository.get(ref) == snapshot

    with psycopg.connect(postgres_dsn) as connection:
        snapshot_row = connection.execute(
            "SELECT radio_count, chain_count FROM hardware_snapshot"
        ).fetchone()
        radios = connection.execute(
            "SELECT radio_index, radio_id FROM hardware_radio ORDER BY radio_index"
        ).fetchall()
        chains = connection.execute(
            """
            SELECT chain_index, receiver_chain_id, lnb_id,
                   valid_from_utc_ns, valid_until_utc_ns
            FROM hardware_receiver_chain ORDER BY chain_index
            """
        ).fetchall()
    assert snapshot_row == (1, 2)
    assert radios == [(0, "radio_v5")]
    assert chains == [
        (0, "rx_v5_0", "lnb-a", 0, 100),
        (1, "rx_v5_0", "lnb-b", 100, None),
    ]


@pytest.mark.integration
def test_hardware_identity_and_idempotency_conflicts_fail_closed(
    postgres_dsn: str, tmp_path
) -> None:
    snapshot = _snapshot()
    repository = _repository(postgres_dsn, tmp_path / "cas")
    repository.publish(snapshot, idempotency_key="hardware:stable")

    with pytest.raises(HardwareSnapshotConflictError):
        repository.publish(snapshot, idempotency_key="hardware:different-key")


@pytest.mark.integration
def test_hardware_reader_requires_exact_ref_and_projection(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    ref = repository.publish(_snapshot(), idempotency_key="hardware:exact")
    assert repository.resolve_ref(ref.snapshot_id) == ref
    wrong = HardwareMetadataSnapshotRef(ref.snapshot_id, Digest.sha256(b"wrong"))

    with pytest.raises(HardwareSnapshotNotFoundError, match="exactly"):
        repository.get(wrong)
    with pytest.raises(HardwareSnapshotNotFoundError, match="requested ID"):
        repository.resolve_ref(HardwareSnapshotId("hw_missing"))

    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("UPDATE hardware_receiver_chain SET lnb_id = 'tampered'")
    with pytest.raises(HardwareSnapshotIntegrityError, match="projection"):
        repository.get(ref)


@pytest.mark.integration
def test_hardware_receiver_must_belong_to_snapshot_radio(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    repository.publish(_snapshot(), idempotency_key="hardware:membership")

    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        connection.execute(
            """
            INSERT INTO hardware_receiver_chain
                (snapshot_id, chain_index, receiver_chain_id, radio_id,
                 radio_channel, lnb_id, valid_from_utc_ns)
            VALUES ('hw_v5_authority', 3, 'rx_unknown', 'radio_unknown',
                    0, 'lnb-unknown', 0)
            """
        )


@pytest.mark.integration
def test_catalog_rejects_overlapping_receiver_effective_dates(
    postgres_dsn: str, tmp_path
) -> None:
    repository = _repository(postgres_dsn, tmp_path / "cas")
    repository.publish(_snapshot(), idempotency_key="hardware:effective-dates")

    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.ExclusionViolation),
    ):
        connection.execute(
            """
            INSERT INTO hardware_receiver_chain
                (snapshot_id, chain_index, receiver_chain_id, radio_id,
                 radio_channel, lnb_id, valid_from_utc_ns, valid_until_utc_ns)
            VALUES ('hw_v5_authority', 3, 'rx_v5_0', 'radio_v5',
                    0, 'lnb-overlap', 50, 150)
            """
        )


@pytest.mark.integration
def test_concurrent_exact_hardware_publication_exposes_one_snapshot(
    postgres_dsn: str, tmp_path
) -> None:
    snapshot = _snapshot()

    def publish(_index: int):
        return _repository(postgres_dsn, tmp_path / "cas", role=True).publish(
            snapshot, idempotency_key="hardware:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(6)))
    assert all(result == results[0] for result in results)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM hardware_snapshot"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM hardware_receiver_chain"
        ).fetchone() == (2,)
