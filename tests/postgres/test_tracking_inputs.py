from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters import tracking_input_postgres_sql as tracking_sql
from leo_flow.adapters.tracking_input_postgres import (
    PostgresTrackingInputCatalog,
    TrackingInputConflictError,
    TrackingInputObjectCollisionError,
    _parameters,
)
from leo_flow.analysis.model.tracking_input_codec import encode_tracking_input
from leo_flow.analysis.model.tracking_input_persistence import (
    DurableTrackingInputRepository,
    tracking_input_projection,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    TrackingInputSnapshot,
    TrackingInputSnapshotRef,
)
from leo_flow.maintenance.postgres_gc import PostgresGarbageCollectionCatalog
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.model_analysis.test_tracking_input_builder import _case


def _connect(
    postgres_dsn: str, *, role: bool = False
) -> psycopg.Connection[dict[str, object]]:
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    if role:
        connection.execute("SET ROLE leo_analysis")
    return connection


def _catalog(postgres_dsn: str, *, role: bool = False) -> PostgresTrackingInputCatalog:
    return PostgresTrackingInputCatalog(lambda: _connect(postgres_dsn, role=role))


def _repository(
    postgres_dsn: str, root: Path, *, role: bool = False
) -> DurableTrackingInputRepository:
    return DurableTrackingInputRepository(
        FileSystemBlobStore(root), _catalog(postgres_dsn, role=role)
    )


def _insert_object(
    connection: psycopg.Connection,
    digest: Digest,
    *,
    byte_count: int = 1,
    media_type: str = "application/octet-stream",
    format_id: str = "fixture-v1",
    locator: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO object_blob
            (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            digest.algorithm.value,
            digest.value,
            byte_count,
            media_type,
            format_id,
            locator or f"fixture:sha256:{digest.value}",
        ),
    )


def _seed_authorities(postgres_dsn: str) -> TrackingInputSnapshot:
    case = _case()
    snapshot = case.freeze()
    source = case.source
    entry = snapshot.entries[0]
    recording = source.published_recording.recording_object
    PostgresRecordingCatalog(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    ).publish(recording, idempotency_key="tracking-source-recording")

    feature_ref = case.dataset.members[0].feature_set_ref
    with psycopg.connect(postgres_dsn) as connection:
        _insert_object(
            connection,
            feature_ref.bundle_ref.digest,
            byte_count=feature_ref.bundle_ref.byte_count,
            media_type=feature_ref.bundle_ref.media_type,
            format_id=feature_ref.bundle_ref.format_id,
            locator=feature_ref.bundle_ref.locator,
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
                    'sha256', %s, 1, 0, 'tracking-source-feature')
            """,
            (
                str(feature_ref.feature_set_id),
                str(feature_ref.analysis_run_id),
                entry.measurement.recording_id,
                entry.recording_identity_digest.value,
                Digest.sha256(b"tracking-feature-request").value,
                feature_ref.bundle_ref.digest.value,
            ),
        )

        dataset_bundle_digest = Digest.sha256(b"tracking-dataset-bundle")
        _insert_object(
            connection,
            dataset_bundle_digest,
            media_type="application/json",
            format_id="dataset-snapshot-bundle-v0.1",
        )
        dataset = snapshot.durable_dataset
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
                    'doppler', 'tracking-fixture', 2000, true, '[]'::jsonb,
                    1, 'tracking-source-dataset')
            """,
            (
                str(dataset.snapshot_id),
                dataset.feature_membership_digest.value,
                dataset.snapshot_digest.value,
                dataset_bundle_digest.value,
            ),
        )
        connection.execute(
            """
            INSERT INTO dataset_member
                (snapshot_id, member_index, feature_set_id, analysis_run_id,
                 feature_digest_algorithm, feature_digest_value,
                 feature_byte_count, feature_media_type, feature_format_id,
                 feature_locator, split_group_id, split, role, truth)
            VALUES (%s, 0, %s, %s, 'sha256', %s, %s, %s, %s, %s,
                    'recording-family-a', 'train', 'scored_truth', '{}'::jsonb)
            """,
            (
                str(dataset.snapshot_id),
                str(feature_ref.feature_set_id),
                str(feature_ref.analysis_run_id),
                feature_ref.bundle_ref.digest.value,
                feature_ref.bundle_ref.byte_count,
                feature_ref.bundle_ref.media_type,
                feature_ref.bundle_ref.format_id,
                feature_ref.bundle_ref.locator,
            ),
        )

        hardware_ref = entry.hardware_link.hardware_snapshot_ref
        _insert_object(connection, hardware_ref.digest)
        connection.execute(
            """
            INSERT INTO hardware_snapshot
                (snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
                 bundle_digest_algorithm, bundle_digest_value, station_id,
                 radio_count, chain_count, idempotency_key)
            VALUES (%s, 'sha256', %s, 'sha256', %s, 'station_tracking',
                    1, 0, 'tracking-source-hardware')
            """,
            (
                str(hardware_ref.snapshot_id),
                hardware_ref.digest.value,
                hardware_ref.digest.value,
            ),
        )
        connection.execute(
            """
            INSERT INTO recording_hardware_link
                (link_id, recording_id, recording_identity_digest_algorithm,
                 recording_identity_digest_value, hardware_snapshot_id,
                 hardware_snapshot_digest_algorithm,
                 hardware_snapshot_digest_value, link_digest_algorithm,
                 link_digest_value, idempotency_key)
            VALUES (%s, %s, 'sha256', %s, %s, 'sha256', %s,
                    'sha256', %s, 'tracking-source-hardware-link')
            """,
            (
                entry.hardware_link.link_id,
                str(entry.measurement.recording_id),
                entry.recording_identity_digest.value,
                str(hardware_ref.snapshot_id),
                hardware_ref.digest.value,
                entry.hardware_link.link_digest.value,
            ),
        )

        ephemeris_ref = entry.ephemeris_link.selection.snapshot_ref
        provenance_digest = Digest.sha256(b"tracking-ephemeris-provenance")
        for digest in (
            ephemeris_ref.raw_digest,
            ephemeris_ref.normalized_digest,
            provenance_digest,
        ):
            _insert_object(connection, digest)
        connection.execute(
            """
            INSERT INTO ephemeris_snapshot
                (snapshot_id, retrieval_id, source, scope, retrieved_at_utc_ns,
                 raw_digest_algorithm, raw_digest_value,
                 normalized_digest_algorithm, normalized_digest_value,
                 provenance_digest_algorithm, provenance_digest_value,
                 parser_artifact_id, parser_digest_algorithm, parser_digest_value,
                 parser_schema_id, parser_schema_version, satellite_count,
                 norad_id_set_digest_algorithm, norad_id_set_digest_value,
                 element_epoch_min_utc_ns, element_epoch_max_utc_ns,
                 validation_policy_artifact_id,
                 validation_policy_digest_algorithm,
                 validation_policy_digest_value, validation_policy_schema_id,
                 validation_policy_schema_version, validation_reason_codes,
                 attribution, request_spec_digest)
            VALUES (%s, 'ephret_tracking', %s, %s, 1100,
                    'sha256', %s, 'sha256', %s, 'sha256', %s,
                    'tracking-parser', 'sha256', %s,
                    'org.leo-flow.tracking-parser', '0.1', 1,
                    'sha256', %s, 1000, 1000,
                    'tracking-validation', 'sha256', %s,
                    'org.leo-flow.tracking-validation', '0.1', '[]'::jsonb,
                    'fixture', %s)
            """,
            (
                str(ephemeris_ref.snapshot_id),
                ephemeris_ref.source.value,
                entry.ephemeris_link.scope,
                ephemeris_ref.raw_digest.value,
                ephemeris_ref.normalized_digest.value,
                provenance_digest.value,
                Digest.sha256(b"tracking-parser").value,
                Digest.sha256(b"tracking-norad-set").value,
                Digest.sha256(b"tracking-validation").value,
                Digest.sha256(b"tracking-request").value,
            ),
        )
        selection = entry.ephemeris_link.selection
        policy_ref = selection.policy_ref
        assert policy_ref.schema is not None
        connection.execute(
            """
            INSERT INTO recording_ephemeris_link
                (link_id, recording_id, recording_identity_digest_algorithm,
                 recording_identity_digest_value, recording_started_utc_ns,
                 recording_finished_utc_ns, source, scope, selection_policy,
                 policy_artifact_id, policy_digest_algorithm, policy_digest_value,
                 policy_schema_id, policy_schema_version, as_of_utc_ns,
                 snapshot_id, raw_digest_algorithm, raw_digest_value,
                 normalized_digest_algorithm, normalized_digest_value,
                 link_digest_algorithm, link_digest_value, idempotency_key)
            VALUES (%s, %s, 'sha256', %s, %s, %s, %s, %s, %s,
                    %s, 'sha256', %s, %s, %s, %s,
                    %s, 'sha256', %s, 'sha256', %s,
                    'sha256', %s, 'tracking-source-ephemeris-link')
            """,
            (
                entry.ephemeris_link.link_id,
                str(entry.measurement.recording_id),
                entry.recording_identity_digest.value,
                int(entry.recording_interval.started_utc_ns),
                int(entry.recording_interval.finished_utc_ns),
                selection.source.value,
                entry.ephemeris_link.scope,
                selection.policy.value,
                policy_ref.artifact_id,
                policy_ref.digest.value,
                policy_ref.schema.schema_id,
                str(policy_ref.schema.version),
                int(selection.as_of_utc_ns),
                str(ephemeris_ref.snapshot_id),
                ephemeris_ref.raw_digest.value,
                ephemeris_ref.normalized_digest.value,
                entry.ephemeris_link.link_digest.value,
            ),
        )
    return cast(TrackingInputSnapshot, snapshot)


@pytest.mark.integration
def test_publication_is_exact_idempotent_and_locator_independent(
    postgres_dsn: str, tmp_path: Path
) -> None:
    snapshot = _seed_authorities(postgres_dsn)
    repository = _repository(postgres_dsn, tmp_path / "cas")

    ref = repository.publish(snapshot, idempotency_key="tracking:stable")
    assert repository.publish(snapshot, idempotency_key="tracking:stable") == ref
    view = repository.get_by_identity(ref.identity())
    assert view.ref == ref
    assert view.snapshot == snapshot

    relocated_request = replace(
        ref, bundle_ref=replace(ref.bundle_ref, locator="cas:relocated-request")
    )
    cataloged = _catalog(postgres_dsn).get(relocated_request)
    assert cataloged is not None
    assert cataloged.projection.ref == ref
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tracking_input_snapshot"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM tracking_input_entry"
        ).fetchone() == (len(snapshot.entries),)


@pytest.mark.integration
def test_identity_idempotency_object_and_authority_conflicts_fail_closed(
    postgres_dsn: str, tmp_path: Path
) -> None:
    snapshot = _seed_authorities(postgres_dsn)
    repository = _repository(postgres_dsn, tmp_path / "cas")
    ref = repository.publish(snapshot, idempotency_key="tracking:stable")
    projection = tracking_input_projection(snapshot, ref)

    with pytest.raises(TrackingInputConflictError):
        _catalog(postgres_dsn).publish(
            projection, idempotency_key="tracking:different-key"
        )
    moved_projection = replace(
        projection,
        ref=replace(
            ref, bundle_ref=replace(ref.bundle_ref, locator="cas:other-location")
        ),
    )
    with pytest.raises(TrackingInputObjectCollisionError):
        _catalog(postgres_dsn).publish(
            moved_projection, idempotency_key="tracking:moved-object"
        )

    absent = replace(
        projection,
        entries=(
            replace(
                projection.entries[0],
                hardware_link_digest=Digest.sha256(b"substituted-hardware-link"),
            ),
        ),
    )
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("TRUNCATE tracking_input_entry, tracking_input_snapshot")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _catalog(postgres_dsn).publish(absent, idempotency_key="tracking:substituted")


@pytest.mark.integration
def test_concurrent_role_publication_exposes_one_complete_snapshot(
    postgres_dsn: str, tmp_path: Path
) -> None:
    snapshot = _seed_authorities(postgres_dsn)

    def publish(_index: int) -> TrackingInputSnapshotRef:
        return _repository(postgres_dsn, tmp_path / "cas", role=True).publish(
            snapshot, idempotency_key="tracking:concurrent"
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        refs = list(executor.map(publish, range(6)))
    assert all(ref == refs[0] for ref in refs)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tracking_input_snapshot"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM tracking_input_entry"
        ).fetchone() == (len(snapshot.entries),)


@pytest.mark.integration
def test_roles_have_read_and_publish_function_but_no_table_mutation(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_analysis', 'tracking_input_snapshot', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'tracking_input_snapshot', 'INSERT'),
                   has_table_privilege(
                       'leo_analysis', 'tracking_input_entry', 'DELETE'),
                   has_function_privilege(
                       'leo_analysis', 'publish_tracking_input_snapshot(jsonb)',
                       'EXECUTE'),
                   has_table_privilege(
                       'leo_dashboard', 'tracking_input_snapshot', 'SELECT'),
                   has_function_privilege(
                       'leo_dashboard', 'publish_tracking_input_snapshot(jsonb)',
                       'EXECUTE'),
                   has_table_privilege(
                       'leo_capture', 'tracking_input_snapshot', 'SELECT')
            """
        ).fetchone()
    assert privileges == (True, False, False, True, True, False, False)

    with (
        _connect(postgres_dsn, role=True) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(
            "INSERT INTO tracking_input_snapshot (snapshot_id) VALUES ('trackinput_00000000000000000000000000000000')"
        )


@pytest.mark.integration
def test_tracking_bundle_is_a_queryable_live_retention_reference(
    postgres_dsn: str, tmp_path: Path
) -> None:
    snapshot = _seed_authorities(postgres_dsn)
    ref = _repository(postgres_dsn, tmp_path / "cas").publish(
        snapshot, idempotency_key="tracking:retention"
    )
    digest = ref.bundle_ref.digest.value
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            """
            SELECT reference_kind, owner_id
              FROM object_blob_live_reference
             WHERE digest_algorithm = 'sha256' AND digest_value = %s
            """,
            (digest,),
        ).fetchall() == [("tracking_input_snapshot.bundle", ref.snapshot_id)]
        connection.execute(
            """
            INSERT INTO object_retention_policy
                (policy_id, retain_for_seconds, grace_period_seconds,
                 allow_remote_delete, rationale)
            VALUES ('tracking-expire', 0, 1, true, 'tracking integration fixture')
            """
        )
        connection.execute(
            """
            INSERT INTO object_retention_assignment
                (digest_algorithm, digest_value, policy_id, assigned_at, assigned_by)
            VALUES ('sha256', %s, 'tracking-expire',
                    clock_timestamp() - interval '2 seconds', 'integration-test')
            """,
            (digest,),
        )
        status = connection.execute(
            """
            SELECT live_reference_count
              FROM object_retention_status
             WHERE digest_algorithm = 'sha256' AND digest_value = %s
            """,
            (digest,),
        ).fetchone()
        assert status == (1,)
        assert connection.execute(
            "SELECT count(*) FROM object_gc_candidate WHERE digest_value = %s",
            (digest,),
        ).fetchone() == (0,)


@pytest.mark.integration
def test_tracking_publication_wins_race_with_gc_claim(postgres_dsn: str) -> None:
    snapshot = _seed_authorities(postgres_dsn)
    payload = encode_tracking_input(snapshot)
    bundle = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        TRACKING_INPUT_MEDIA_TYPE,
        TRACKING_INPUT_FORMAT_ID,
        "fixture:tracking-race",
    )
    ref = TrackingInputSnapshotRef(
        snapshot.snapshot_id,
        snapshot.snapshot_digest,
        snapshot.membership_digest,
        bundle,
    )
    projection = tracking_input_projection(snapshot, ref)
    parameters = _parameters(projection, "tracking:gc-race")
    with psycopg.connect(postgres_dsn) as connection:
        _insert_object(
            connection,
            bundle.digest,
            byte_count=bundle.byte_count,
            media_type=bundle.media_type,
            format_id=bundle.format_id,
            locator=bundle.locator,
        )
        connection.execute(
            """
            INSERT INTO object_retention_policy
                (policy_id, retain_for_seconds, grace_period_seconds,
                 allow_remote_delete, rationale)
            VALUES ('tracking-race-expire', 0, 1, true,
                    'tracking publication race fixture')
            """
        )
        connection.execute(
            """
            INSERT INTO object_retention_assignment
                (digest_algorithm, digest_value, policy_id, assigned_at, assigned_by)
            VALUES ('sha256', %s, 'tracking-race-expire',
                    clock_timestamp() - interval '2 seconds', 'integration-test')
            """,
            (bundle.digest.value,),
        )

    inserted = threading.Event()
    release = threading.Event()

    def publish_reference() -> None:
        with _connect(postgres_dsn, role=True) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(tracking_sql.REGISTER_OBJECT_SQL, parameters)
                cursor.execute(tracking_sql.PUBLISH_SQL, parameters)
                assert cursor.fetchone() == {"inserted": True}
            inserted.set()
            assert release.wait(5)

    thread = threading.Thread(target=publish_reference)
    thread.start()
    assert inserted.wait(5)
    gc = PostgresGarbageCollectionCatalog(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    candidate = next(
        item
        for item in gc.candidates(as_of_utc_ns=time.time_ns(), limit=100)
        if item.digest == bundle.digest
    )
    timer = threading.Timer(0.2, release.set)
    timer.start()
    claimed = gc.claim(
        candidate,
        claim_token="tracking-losing-claim",
        claimed_at_utc_ns=time.time_ns(),
        claim_expires_at_utc_ns=time.time_ns() + 10_000_000_000,
    )
    thread.join(5)
    timer.cancel()
    assert not thread.is_alive()
    assert claimed is None
