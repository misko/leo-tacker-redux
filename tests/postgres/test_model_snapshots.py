from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.model_postgres_catalog import (
    ModelDatasetMismatchError,
    ModelReleaseConflictError,
    ModelSnapshotConflictError,
    PostgresModelSnapshotCatalog,
)
from leo_flow.analysis.model import (
    DurableModelSnapshotRepository,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
)
from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.contracts.model import ModelApproval, ModelSnapshotRef
from leo_flow.storage.filesystem import FileSystemBlobStore, IdempotencyConflictError
from tests.model_analysis.fakes import (
    FakeEphemerisReader,
    FakeFeatureSetReader,
    FakeHardwareReader,
    dataset,
    execution_context,
    feature_set,
    hardware_snapshot,
    request,
)


def _fixture():
    first = feature_set(101, (("rx_0", 10.0, 1.0),))
    second = feature_set(102, (("rx_0", 20.0, 1.0),))
    snapshot = dataset((first[0], second[0]))
    hardware = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    model_request = request(snapshot, config, (hardware[0],))
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        model_request,
        FakeFeatureSetReader((first, second)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hardware,)),
    )
    return model_request, bundle, snapshot


def _seed_authoritative_dataset(postgres_dsn: str, snapshot) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        for index, feature_ref in enumerate(snapshot.ordered_feature_set_refs):
            data_digest = Digest.sha256(f"model-recording-data-{index}".encode())
            metadata_digest = Digest.sha256(
                f"model-recording-metadata-{index}".encode()
            )
            for digest, kind in ((data_digest, "data"), (metadata_digest, "metadata")):
                connection.execute(
                    """
                    INSERT INTO object_blob
                        (digest_algorithm, digest_value, byte_count, media_type,
                         format_id, locator)
                    VALUES ('sha256', %s, 1, 'application/octet-stream', %s, %s)
                    """,
                    (
                        digest.value,
                        f"recording-{kind}-v1",
                        f"fixture://model/recording/{index}/{kind}",
                    ),
                )
            recording_id = f"rec_model_{index}"
            connection.execute(
                """
                INSERT INTO recording
                    (recording_id, data_digest_value, metadata_digest_value,
                     manifest_digest_value, idempotency_key, state)
                VALUES (%s, %s, %s, %s, %s, 'published')
                """,
                (
                    recording_id,
                    data_digest.value,
                    metadata_digest.value,
                    Digest.sha256(f"model-manifest-{index}".encode()).value,
                    f"model-recording:{index}",
                ),
            )
            object_ref = feature_ref.bundle_ref
            connection.execute(
                """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    object_ref.digest.algorithm.value,
                    object_ref.digest.value,
                    object_ref.byte_count,
                    object_ref.media_type,
                    object_ref.format_id,
                    object_ref.locator,
                ),
            )
            connection.execute(
                """
                INSERT INTO feature_set
                    (feature_set_id, analysis_run_id, recording_id,
                     input_recording_digest_algorithm, input_recording_digest_value,
                     request_digest_algorithm, request_digest_value,
                     bundle_digest_algorithm, bundle_digest_value,
                     observation_count, method_score_count, idempotency_key)
                VALUES (%s, %s, %s, 'sha256', %s, 'sha256', %s,
                        'sha256', %s, 1, 0, %s)
                """,
                (
                    str(feature_ref.feature_set_id),
                    str(feature_ref.analysis_run_id),
                    recording_id,
                    Digest.sha256(f"model-recording-identity-{index}".encode()).value,
                    Digest.sha256(f"model-feature-request-{index}".encode()).value,
                    object_ref.digest.value,
                    f"model-feature:{index}",
                ),
            )
        dataset_object = Digest.sha256(b"model-dataset-object")
        dataset_snapshot_digest = Digest.sha256(b"model-dataset-snapshot")
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type,
                 format_id, locator)
            VALUES ('sha256', %s, 1, 'application/json',
                    'dataset-snapshot-bundle-v0.1', 'fixture://model/dataset')
            """,
            (dataset_object.value,),
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
            VALUES (%s, 'sha256', %s, 'sha256', %s, 'sha256', %s,
                    'sample-quality', 'model-fixture', %s, true, '[]'::jsonb,
                    %s, 'model-dataset')
            """,
            (
                str(snapshot.snapshot_id),
                snapshot.membership_digest.value,
                dataset_snapshot_digest.value,
                dataset_object.value,
                int(snapshot.selection_cutoff_utc_ns),
                len(snapshot.ordered_feature_set_refs),
            ),
        )
        for index, feature_ref in enumerate(snapshot.ordered_feature_set_refs):
            object_ref = feature_ref.bundle_ref
            connection.execute(
                """
                INSERT INTO dataset_member
                    (snapshot_id, member_index, feature_set_id, analysis_run_id,
                     feature_digest_algorithm, feature_digest_value,
                     feature_byte_count, feature_media_type, feature_format_id,
                     feature_locator, split_group_id, split, role, truth)
                VALUES (%s, %s, %s, %s, 'sha256', %s, %s, %s, %s, %s,
                        %s, 'train', 'context_only', '{}'::jsonb)
                """,
                (
                    str(snapshot.snapshot_id),
                    index,
                    str(feature_ref.feature_set_id),
                    str(feature_ref.analysis_run_id),
                    object_ref.digest.value,
                    object_ref.byte_count,
                    object_ref.media_type,
                    object_ref.format_id,
                    object_ref.locator,
                    f"group-{index}",
                ),
            )


def _repository(postgres_dsn: str, root, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return DurableModelSnapshotRepository(
        FileSystemBlobStore(root),
        PostgresModelSnapshotCatalog(connect),
    )


@pytest.mark.integration
def test_dataset_to_model_publication_is_exact_and_idempotent(
    postgres_dsn: str, tmp_path
) -> None:
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    repository = _repository(postgres_dsn, tmp_path / "cas")

    first = repository.publish(model_request, bundle, idempotency_key="model:stable")
    assert (
        repository.publish(model_request, bundle, idempotency_key="model:stable")
        == first
    )
    with repository.open(first) as view:
        assert view.ref == first
        assert view.bundle() == bundle
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            1,
        )


@pytest.mark.integration
def test_model_identity_key_and_bundle_conflicts_fail_closed(
    postgres_dsn: str, tmp_path
) -> None:
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    repository.publish(model_request, bundle, idempotency_key="model:stable")

    with pytest.raises(ModelSnapshotConflictError):
        repository.publish(model_request, bundle, idempotency_key="model:new-key")
    with pytest.raises(IdempotencyConflictError):
        repository.publish(
            model_request,
            replace(bundle, warnings=bundle.warnings + ("different",)),
            idempotency_key="model:stable",
        )


@pytest.mark.integration
def test_model_publication_requires_exact_dataset_and_member_provenance(
    postgres_dsn: str, tmp_path
) -> None:
    model_request, bundle, snapshot = _fixture()
    repository = _repository(postgres_dsn, tmp_path / "cas")
    with pytest.raises(ModelDatasetMismatchError, match="authoritative dataset"):
        repository.publish(model_request, bundle, idempotency_key="model:orphan")
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    substituted = replace(
        bundle,
        provenance=replace(
            bundle.provenance,
            input_digests=(
                bundle.dataset_membership_digest,
                Digest.sha256(b"substituted-member"),
                bundle.provenance.input_digests[-1],
            ),
        ),
    )
    with pytest.raises(ModelDatasetMismatchError, match="exact dataset members"):
        repository.publish(
            model_request, substituted, idempotency_key="model:substituted"
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            0,
        )


@pytest.mark.integration
def test_release_history_switches_alias_atomically_and_is_idempotent(
    postgres_dsn: str, tmp_path
) -> None:
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    ref = repository.publish(model_request, bundle, idempotency_key="model:release")
    first_approval = ModelApproval("reviewer", UtcNs(10), "initial approval")
    first = repository.release(
        ref, "current", first_approval, idempotency_key="release:first"
    )
    assert (
        repository.release(
            ref, "current", first_approval, idempotency_key="release:first"
        )
        == first
    )
    second_approval = ModelApproval("reviewer", UtcNs(20), "new review event")
    second = repository.release(
        ref, "current", second_approval, idempotency_key="release:second"
    )
    assert second != first
    assert repository.get_release("current") == second
    with pytest.raises(ModelReleaseConflictError):
        repository.release(
            ref, "candidate", second_approval, idempotency_key="release:second"
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_release").fetchone() == (
            2,
        )


@pytest.mark.integration
def test_model_reader_requires_full_exact_ref(postgres_dsn: str, tmp_path) -> None:
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    ref = repository.publish(model_request, bundle, idempotency_key="model:exact")
    moved = ModelSnapshotRef(
        ref.model_snapshot_id,
        ref.model_run_id,
        replace(ref.bundle_ref, locator="opaque://substituted"),
    )
    from leo_flow.analysis.model import ModelSnapshotNotFoundError

    with pytest.raises(ModelSnapshotNotFoundError), repository.open(moved):
        pass


@pytest.mark.integration
def test_concurrent_exact_model_publication_exposes_one_row(
    postgres_dsn: str, tmp_path
) -> None:
    model_request, bundle, snapshot = _fixture()
    _seed_authoritative_dataset(postgres_dsn, snapshot)

    def publish(_index: int):
        return _repository(postgres_dsn, tmp_path / "cas", role=True).publish(
            model_request, bundle, idempotency_key="model:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(6)))
    assert all(result == results[0] for result in results)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM model_snapshot").fetchone() == (
            1,
        )
