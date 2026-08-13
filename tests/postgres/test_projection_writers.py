from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.adapters.dashboard_projection_postgres import (
    PostgresAnalysisProjectionWriter,
    PostgresCaptureProjectionWriter,
)
from leo_flow.application.projection_writers import (
    FeatureProjectionCommand,
    ModelProjectionCommand,
    ModelReleaseProjectionCommand,
    ProjectionConflict,
    RecordingProjectionCommand,
    TrackProjectionCommand,
)
from leo_flow.contracts.core import (
    Digest,
    FeatureId,
    RadioId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import StorageHealth, TrackView
from leo_flow.contracts.model import ModelApproval, ModelRelease
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.storage.postgres_catalog import connection_factory
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    model_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def _role_connection(
    postgres_dsn: str, role: str
) -> psycopg.Connection[dict[str, object]]:
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    connection.execute(f"SET ROLE {role}")
    return connection


def _catalog_recording(postgres_dsn: str, published: PublishedRecordingRef) -> None:
    ref = published.recording_object
    with psycopg.connect(postgres_dsn) as connection:
        for obj in (ref.data_object, ref.metadata_object):
            connection.execute(
                """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    obj.digest.algorithm.value,
                    obj.digest.value,
                    obj.byte_count,
                    obj.media_type,
                    obj.format_id,
                    obj.locator,
                ),
            )
        connection.execute(
            """
            INSERT INTO recording
                (recording_id, data_digest_value, metadata_digest_value,
                 manifest_digest_value, idempotency_key, state)
            VALUES (%s, %s, %s, %s, %s, 'published')
            """,
            (
                str(ref.recording_id),
                ref.data_object.digest.value,
                ref.metadata_object.digest.value,
                ref.manifest_digest.value,
                f"projection:{ref.recording_id}",
            ),
        )


@pytest.mark.integration
def test_capture_projection_is_idempotent_append_only_and_atomic(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(1)
    published = published_recording(manifest)
    _catalog_recording(postgres_dsn, published)
    writer = PostgresCaptureProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_capture")
    )
    command = RecordingProjectionCommand(manifest, published, True)
    first = writer.project_recording(command)
    assert writer.project_recording(command) == first
    corrected = writer.project_recording(
        replace(command, recording_object_available=False)
    )
    assert corrected.projection_sequences[0] != first.projection_sequences[0]
    assert corrected.projection_sequences[1:] == first.projection_sequences[1:]
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_recording_projection"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM dashboard_activity_projection"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT analysis_state FROM dashboard_recording_projection
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone() == ("pending",)

    other = recording_manifest(2)
    conflicting_activity = replace(
        other.activities[0], activity_id=manifest.activities[0].activity_id
    )
    other = replace(other, activities=(conflicting_activity,))
    other_published = published_recording(other)
    _catalog_recording(postgres_dsn, other_published)
    with pytest.raises(ProjectionConflict, match="activity"):
        writer.project_recording(
            RecordingProjectionCommand(other, other_published, True)
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM dashboard_recording_projection
            WHERE recording_id = %s
            """,
            (str(other.recording_id),),
        ).fetchone() == (0,)


@pytest.mark.integration
def test_concurrent_recording_retry_converges_on_one_projection(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(7)
    published = published_recording(manifest)
    _catalog_recording(postgres_dsn, published)
    command = RecordingProjectionCommand(manifest, published, True)

    def project() -> tuple[int, ...]:
        return (
            PostgresCaptureProjectionWriter(
                lambda: _role_connection(postgres_dsn, "leo_capture")
            )
            .project_recording(command)
            .projection_sequences
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(lambda _: project(), range(2)))
    assert receipts[0] == receipts[1]
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_recording_projection"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM dashboard_activity_projection"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM dashboard_capture_projection_identity"
        ).fetchone() == (2,)


@pytest.mark.integration
def test_database_fault_rolls_back_whole_recording_projection(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(3)
    published = published_recording(manifest)
    _catalog_recording(postgres_dsn, published)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION fail_activity_projection() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected fault'; END $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER injected_activity_fault
            BEFORE INSERT ON dashboard_activity_projection
            FOR EACH ROW EXECUTE FUNCTION fail_activity_projection()
            """
        )
    writer = PostgresCaptureProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_capture")
    )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected fault"):
            writer.project_recording(
                RecordingProjectionCommand(manifest, published, True)
            )
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM dashboard_recording_projection"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM dashboard_activity_projection"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM dashboard_capture_projection_identity"
            ).fetchone() == (0,)
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                "DROP TRIGGER injected_activity_fault ON dashboard_activity_projection"
            )
            connection.execute("DROP FUNCTION fail_activity_projection()")


@pytest.mark.integration
def test_analysis_projections_validate_and_retry_without_duplicates(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(4)
    recording = published_recording(manifest)
    _catalog_recording(postgres_dsn, recording)
    capture = PostgresCaptureProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_capture")
    )
    capture.project_recording(RecordingProjectionCommand(manifest, recording, True))
    writer = PostgresAnalysisProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_analysis")
    )
    features, feature_ref = feature_bundle_and_ref(
        recording.recording_object, manifest, 4
    )
    feature_command = FeatureProjectionCommand(features, feature_ref, recording)
    feature_receipt = writer.project_features(feature_command)
    assert writer.project_features(feature_command) == feature_receipt
    assert len(feature_receipt.projection_sequences) == 2
    # A late capture retry cannot overwrite the analysis-owned completion state.
    capture.project_recording(RecordingProjectionCommand(manifest, recording, True))
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_recording_projection"
        ).fetchone() == (2,)
        assert connection.execute(
            """
            SELECT analysis_state FROM dashboard_recording_projection
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone() == ("complete",)

    model, model_ref = model_bundle_and_ref(4)
    model_command = ModelProjectionCommand(model, model_ref)
    model_receipt = writer.project_model(model_command)
    assert writer.project_model(model_command) == model_receipt
    relocated_model_ref = replace(
        model_ref,
        bundle_ref=replace(model_ref.bundle_ref, locator="object://relocated/model/4"),
    )
    assert (
        writer.project_model(ModelProjectionCommand(model, relocated_model_ref))
        == model_receipt
    )
    release = ModelRelease(
        "production",
        model_ref,
        ModelApproval("test-operator", UtcNs(50), "validated fixture"),
    )
    release_command = ModelReleaseProjectionCommand(model, release)
    release_receipt = writer.project_model_release(release_command)
    assert writer.project_model_release(release_command) == release_receipt
    relocated_release = replace(release, model_ref=relocated_model_ref)
    assert (
        writer.project_model_release(
            ModelReleaseProjectionCommand(model, relocated_release)
        )
        == release_receipt
    )
    staging = replace(release, alias="staging")
    writer.project_model_release(ModelReleaseProjectionCommand(model, staging))
    repository = PostgresDashboardRepository(connection_factory(postgres_dsn))
    assert (
        repository.model_snapshot("production").model_snapshot_id
        == model.model_snapshot_id
    )
    assert (
        repository.model_snapshot("staging").model_snapshot_id
        == model.model_snapshot_id
    )

    track_command = TrackProjectionCommand(
        TrackView("track_projection_4", model.model_snapshot_id, UtcNs(40), UtcNs(60)),
        RadioId("radio_projection"),
        model,
        model_ref,
    )
    track_receipt = writer.project_track(track_command)
    assert writer.project_track(track_command) == track_receipt
    relocated_track = replace(track_command, model_ref=relocated_model_ref)
    assert writer.project_track(relocated_track) == track_receipt
    with pytest.raises(ProjectionConflict, match="authoritative identity"):
        writer.project_track(replace(track_command, radio_id=RadioId("radio_other")))
    health = StorageHealth(True, 1_000, 250)
    health_receipt = writer.project_storage_health(health)
    assert writer.project_storage_health(health) == health_receipt
    changed_health = writer.project_storage_health(StorageHealth(True, 1_000, 200))
    assert changed_health != health_receipt

    with psycopg.connect(postgres_dsn) as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM dashboard_feature_projection),
              (SELECT count(*) FROM dashboard_model_projection),
              (SELECT count(*) FROM dashboard_track_projection),
              (SELECT count(*) FROM dashboard_storage_health_projection)
            """
        ).fetchone()
    assert counts == (1, 3, 1, 2)

    conflicting, conflicting_ref = feature_bundle_and_ref(
        recording.recording_object,
        manifest,
        5,
        feature_id=FeatureId("feature_projection_4"),
        score=0.5,
    )
    with pytest.raises(ProjectionConflict, match="feature"):
        writer.project_features(
            FeatureProjectionCommand(conflicting, conflicting_ref, recording)
        )

    other_model, other_model_ref = model_bundle_and_ref(5)
    other_model = replace(other_model, model_snapshot_id=model.model_snapshot_id)
    other_payload = canonical_json_bytes(other_model)
    other_model_ref = replace(
        other_model_ref,
        model_snapshot_id=model.model_snapshot_id,
        bundle_ref=replace(
            other_model_ref.bundle_ref,
            digest=Digest.sha256(other_payload),
            byte_count=len(other_payload),
        ),
    )
    with pytest.raises(ProjectionConflict, match="authoritative identity"):
        writer.project_model(ModelProjectionCommand(other_model, other_model_ref))

    other_release = replace(
        release,
        approval=ModelApproval("another-operator", UtcNs(51), "same model, new vote"),
    )
    with pytest.raises(ProjectionConflict, match="authoritative identity"):
        writer.project_model_release(
            ModelReleaseProjectionCommand(model, other_release)
        )


@pytest.mark.integration
def test_analysis_identity_fault_rolls_back_dto_and_identity(postgres_dsn: str) -> None:
    manifest = recording_manifest(6)
    recording = published_recording(manifest)
    _catalog_recording(postgres_dsn, recording)
    PostgresCaptureProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_capture")
    ).project_recording(RecordingProjectionCommand(manifest, recording, True))
    bundle, bundle_ref = feature_bundle_and_ref(recording.recording_object, manifest, 6)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION fail_analysis_identity() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'identity fault'; END $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER injected_identity_fault
            BEFORE INSERT ON dashboard_analysis_projection_identity
            FOR EACH ROW EXECUTE FUNCTION fail_analysis_identity()
            """
        )
    writer = PostgresAnalysisProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_analysis")
    )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="identity fault"):
            writer.project_features(
                FeatureProjectionCommand(bundle, bundle_ref, recording)
            )
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM dashboard_feature_projection"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM dashboard_analysis_projection_identity"
            ).fetchone() == (0,)
            assert connection.execute(
                """
                SELECT analysis_state FROM dashboard_recording_projection
                ORDER BY projection_sequence DESC LIMIT 1
                """
            ).fetchone() == ("pending",)
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                "DROP TRIGGER injected_identity_fault ON dashboard_analysis_projection_identity"
            )
            connection.execute("DROP FUNCTION fail_analysis_identity()")


@pytest.mark.integration
def test_completion_fault_rolls_back_features_and_preserves_pending(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(8)
    recording = published_recording(manifest)
    _catalog_recording(postgres_dsn, recording)
    PostgresCaptureProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_capture")
    ).project_recording(RecordingProjectionCommand(manifest, recording, True))
    bundle, bundle_ref = feature_bundle_and_ref(recording.recording_object, manifest, 8)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION fail_recording_completion() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'completion fault'; END $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER injected_completion_fault
            BEFORE INSERT ON dashboard_recording_projection
            FOR EACH ROW WHEN (NEW.analysis_state = 'complete')
            EXECUTE FUNCTION fail_recording_completion()
            """
        )
    writer = PostgresAnalysisProjectionWriter(
        lambda: _role_connection(postgres_dsn, "leo_analysis")
    )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="completion fault"):
            writer.project_features(
                FeatureProjectionCommand(bundle, bundle_ref, recording)
            )
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM dashboard_feature_projection"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM dashboard_analysis_projection_identity"
            ).fetchone() == (0,)
            assert connection.execute(
                """
                SELECT analysis_state FROM dashboard_recording_projection
                ORDER BY projection_sequence DESC
                """
            ).fetchall() == [("pending",)]
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                "DROP TRIGGER injected_completion_fault ON dashboard_recording_projection"
            )
            connection.execute("DROP FUNCTION fail_recording_completion()")


@pytest.mark.integration
def test_database_roles_enforce_capability_scoped_writes(postgres_dsn: str) -> None:
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        _role_connection(postgres_dsn, "leo_capture") as connection,
    ):
        connection.execute(
            """
            INSERT INTO dashboard_feature_projection
                (feature_id, recording_id, method_id, score, score_semantics)
            VALUES ('feature_forbidden', 'rec_missing', 'fft', 0.5, 'score')
            """
        )
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        _role_connection(postgres_dsn, "leo_dashboard") as connection,
    ):
        connection.execute(
            """
            INSERT INTO dashboard_storage_health_projection
                (available, total_bytes, free_bytes)
            VALUES (false, NULL, NULL)
            """
        )
