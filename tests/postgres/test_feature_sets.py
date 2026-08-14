from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest

from leo_flow.adapters.feature_postgres_catalog import (
    FeatureRecordingMismatchError,
    FeatureSetConflictError,
    PostgresFeatureSetCatalog,
    connection_factory,
)
from leo_flow.analysis.recording import DurableFeatureSetRepository
from leo_flow.contracts.core import Digest
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.storage.filesystem import FileSystemBlobStore, IdempotencyConflictError
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.recording_analysis.test_feature_persistence import _fixture


def _repository(postgres_dsn: str, root):
    return DurableFeatureSetRepository(
        FileSystemBlobStore(root),
        PostgresFeatureSetCatalog(connection_factory(postgres_dsn)),
    )


def _publish_recording(postgres_dsn: str):
    request, bundle = _fixture()
    PostgresRecordingCatalog(connection_factory(postgres_dsn)).publish(
        request.recording_object_ref, idempotency_key="feature-source-recording"
    )
    return request, bundle


@pytest.mark.integration
def test_recording_to_feature_publication_is_exact_and_idempotent(
    postgres_dsn: str, tmp_path
) -> None:
    request, bundle = _publish_recording(postgres_dsn)
    repository = _repository(postgres_dsn, tmp_path / "cas")

    first = repository.publish(request, bundle, idempotency_key="feature:stable")
    assert (
        repository.publish(request, bundle, idempotency_key="feature:stable") == first
    )
    with repository.open(first) as view:
        assert view.ref == first
        assert view.bundle() == bundle
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (1,)


@pytest.mark.integration
def test_feature_identity_key_and_bundle_conflicts_fail_closed(
    postgres_dsn: str, tmp_path
) -> None:
    request, bundle = _publish_recording(postgres_dsn)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    repository.publish(request, bundle, idempotency_key="feature:stable")

    with pytest.raises(FeatureSetConflictError):
        repository.publish(request, bundle, idempotency_key="feature:new-key")
    changed = replace(bundle, warnings=("different",))
    with pytest.raises(IdempotencyConflictError):
        repository.publish(request, changed, idempotency_key="feature:stable")


@pytest.mark.integration
def test_feature_publication_requires_exact_cataloged_recording(
    postgres_dsn: str, tmp_path
) -> None:
    request, bundle = _fixture()
    repository = _repository(postgres_dsn, tmp_path / "cas")

    with pytest.raises(FeatureRecordingMismatchError, match="recording catalog"):
        repository.publish(request, bundle, idempotency_key="feature:orphan")
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM object_blob WHERE format_id = 'feature-set-bundle-v0.1'"
        ).fetchone() == (0,)
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


@pytest.mark.integration
def test_feature_reader_requires_full_exact_object_ref(
    postgres_dsn: str, tmp_path
) -> None:
    request, bundle = _publish_recording(postgres_dsn)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    ref = repository.publish(request, bundle, idempotency_key="feature:exact")
    moved = FeatureSetRef(
        ref.feature_set_id,
        ref.analysis_run_id,
        replace(ref.bundle_ref, locator="opaque://substituted"),
    )

    from leo_flow.analysis.recording import FeatureSetNotFoundError

    with pytest.raises(FeatureSetNotFoundError), repository.open(moved):
        pass


@pytest.mark.integration
def test_dataset_member_fk_rejects_unpublished_feature(postgres_dsn: str) -> None:
    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        dataset_digest = Digest.sha256(b"dataset")
        membership_digest = Digest.sha256(b"membership")
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type,
                 format_id, locator)
            VALUES ('sha256', %s, 1, 'application/json',
                    'dataset-snapshot-bundle-v0.1', 'opaque://dataset')
            """,
            (dataset_digest.value,),
        )
        connection.execute(
            """
            INSERT INTO dataset_snapshot
                (snapshot_id, feature_membership_digest_algorithm,
                 feature_membership_digest_value, snapshot_digest_algorithm,
                 snapshot_digest_value, bundle_digest_algorithm,
                 bundle_digest_value, evaluated_method_id, selection_spec,
                 selection_cutoff_utc_ns, promoted, promotion_warnings,
                 member_count, idempotency_key)
            VALUES ('dataset_absent', 'sha256', %s, 'sha256', %s, 'sha256', %s,
                    'method', 'fixture', 0, true, '[]'::jsonb, 1, 'dataset:absent')
            """,
            (membership_digest.value, dataset_digest.value, dataset_digest.value),
        )
        connection.execute(
            """
            INSERT INTO dataset_member
                (snapshot_id, member_index, feature_set_id, analysis_run_id,
                 feature_digest_algorithm, feature_digest_value,
                 feature_byte_count, feature_media_type, feature_format_id,
                 feature_locator, split_group_id, split, role, truth)
            VALUES ('dataset_absent', 0, 'fset_absent', 'arun_absent',
                    'sha256', %s, 1, 'application/json', 'feature-set-bundle-v0.1',
                    'opaque://absent', 'group', 'train', 'context_only', '{}'::jsonb)
            """,
            (Digest.sha256(b"absent").value,),
        )


@pytest.mark.integration
def test_concurrent_exact_feature_publication_exposes_one_row(
    postgres_dsn: str, tmp_path
) -> None:
    request, bundle = _publish_recording(postgres_dsn)

    def publish(_index: int):
        return _repository(postgres_dsn, tmp_path / "cas").publish(
            request, bundle, idempotency_key="feature:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(6)))
    assert all(result == results[0] for result in results)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (1,)
