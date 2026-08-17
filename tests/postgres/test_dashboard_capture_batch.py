from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresBatchAwareAnalysisProjectionWriter,
    PostgresCaptureBatchDashboardRepository,
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.application.projection_writers import (
    AnalysisProjectionWriter,
    FeatureProjectionCommand,
    ProjectionReceipt,
)
from leo_flow.contracts.capture_batch import CaptureBatchMode
from leo_flow.contracts.core import CaptureBatchId, UtcNs, canonical_json_bytes
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchTimeRangeQuery,
    CoordinationClaim,
    DashboardAnalysisState,
)
from leo_flow.dashboard import DashboardNotFound, InvalidCursor
from tests.dashboard._fixtures import (
    BATCH_PEER_FAILED,
    BATCH_READY,
    capture_batches,
)
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    published_recording,
    recording_manifest,
)


def _connect(postgres_dsn: str):
    return lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)


def _role_connect(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


@pytest.mark.integration
def test_publication_is_idempotent_and_appends_only_on_semantic_change(
    postgres_dsn: str,
) -> None:
    complete = capture_batches()[0].view
    initial_attempt = replace(complete.attempts[0], analysis_result_available=False)
    ready = replace(complete, attempts=(initial_attempt, complete.attempts[1]))
    capture_writer = PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_capture")
    )
    first = capture_writer.publish(ready)
    assert capture_writer.publish(ready) == first

    changed = complete
    analysis_writer = PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    )
    second = analysis_writer.publish(changed)
    assert second > first
    assert analysis_writer.publish(changed) == second

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_capture_batch_projection"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM dashboard_capture_attempt_projection"
        ).fetchone() == (4,)
    exact = PostgresCaptureBatchDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard")
    ).capture_batch(BATCH_READY)
    assert exact == changed


@pytest.mark.integration
def test_analysis_retry_transitions_are_allowed_but_available_result_cannot_regress(
    postgres_dsn: str,
) -> None:
    pending = capture_batches()[1].view
    writer = PostgresCaptureBatchProjectionWriter(_connect(postgres_dsn))
    first = writer.publish(pending)
    running_attempt = replace(
        pending.attempts[0], analysis_state=DashboardAnalysisState.RUNNING
    )
    running = replace(pending, attempts=(running_attempt, pending.attempts[1]))
    second = writer.publish(running)
    failed_attempt = replace(
        running_attempt, analysis_state=DashboardAnalysisState.FAILED
    )
    retryable_failure = replace(running, attempts=(failed_attempt, running.attempts[1]))
    third = writer.publish(retryable_failure)
    retried_attempt = replace(
        failed_attempt, analysis_state=DashboardAnalysisState.PENDING
    )
    retried = replace(
        retryable_failure, attempts=(retried_attempt, retryable_failure.attempts[1])
    )
    fourth = writer.publish(retried)
    assert first < second < third < fourth

    complete = capture_batches()[0].view
    writer.publish(complete)
    regressed = replace(
        complete,
        attempts=(
            replace(
                complete.attempts[0],
                analysis_state=DashboardAnalysisState.FAILED,
                analysis_result_available=False,
            ),
            complete.attempts[1],
        ),
    )
    with pytest.raises(psycopg.errors.UniqueViolation, match="cannot regress"):
        writer.publish(regressed)


@pytest.mark.integration
@pytest.mark.parametrize("initial_publisher_role", ["leo_capture", "leo_analysis"])
def test_terminal_capture_replay_preserves_newer_completed_analysis(
    postgres_dsn: str, initial_publisher_role: str
) -> None:
    complete = capture_batches()[0].view
    initial = replace(
        complete,
        attempts=tuple(
            replace(
                attempt,
                analysis_state=DashboardAnalysisState.PENDING,
                analysis_result_available=False,
            )
            for attempt in complete.attempts
        ),
    )
    initial_writer = PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, initial_publisher_role)
    )
    analysis_writer = PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    )

    initial_sequence = initial_writer.publish(initial)
    completed_sequence = analysis_writer.publish(complete)
    assert completed_sequence > initial_sequence
    assert initial_writer.publish(initial) == completed_sequence

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_capture_batch_projection"
        ).fetchone() == (2,)
    exact = PostgresCaptureBatchDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard")
    ).capture_batch(BATCH_READY)
    assert exact == complete


@pytest.mark.integration
def test_stale_initial_replay_cannot_smuggle_other_analysis_changes(
    postgres_dsn: str,
) -> None:
    complete = capture_batches()[0].view
    mixed_latest = replace(
        complete,
        attempts=(
            complete.attempts[0],
            replace(
                complete.attempts[1],
                analysis_state=DashboardAnalysisState.RUNNING,
                analysis_result_available=False,
            ),
        ),
    )
    initial = replace(
        complete,
        attempts=tuple(
            replace(
                attempt,
                analysis_state=DashboardAnalysisState.PENDING,
                analysis_result_available=False,
            )
            for attempt in complete.attempts
        ),
    )
    PostgresCaptureBatchProjectionWriter(
        _role_connect(postgres_dsn, "leo_analysis")
    ).publish(mixed_latest)

    with pytest.raises(psycopg.errors.UniqueViolation, match="analysis changes"):
        PostgresCaptureBatchProjectionWriter(
            _role_connect(postgres_dsn, "leo_capture")
        ).publish(initial)


@pytest.mark.integration
def test_terminal_analysis_change_cannot_rewrite_capture_timing_or_eligibility(
    postgres_dsn: str,
) -> None:
    ready = capture_batches()[0].view
    writer = PostgresCaptureBatchProjectionWriter(_connect(postgres_dsn))
    writer.publish(ready)
    changed_payload = json.loads(canonical_json_bytes(ready))
    changed_payload["observed_start_skew_ns"] = 6
    with (
        psycopg.connect(postgres_dsn, row_factory=dict_row) as connection,
        pytest.raises(psycopg.Error, match="inconsistent|rewrites"),
    ):
        connection.execute(
            "SELECT publish_dashboard_capture_batch(%s::jsonb)",
            (Jsonb(changed_payload),),
        ).fetchone()

    independent = replace(
        ready,
        mode=CaptureBatchMode.INDEPENDENT,
        coordination_claim=CoordinationClaim.NONE,
        maximum_observed_start_skew_ns=None,
    )
    with pytest.raises(psycopg.errors.UniqueViolation, match="immutable intent"):
        writer.publish(independent)


@pytest.mark.integration
def test_recent_batches_are_snapshot_stable_and_exact_rows_preserve_failed_timing(
    postgres_dsn: str,
) -> None:
    writer = PostgresCaptureBatchProjectionWriter(_connect(postgres_dsn))
    views = [row.view for row in capture_batches()]
    for view in views:
        writer.publish(view)
    repository = PostgresCaptureBatchDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard"), page_size=2
    )
    query = CaptureBatchTimeRangeQuery(UtcNs(0), UtcNs(500))
    first = repository.recent_capture_batches(query)
    assert [view.batch_id for view in first.items] == [
        BATCH_READY,
        CaptureBatchId("cbatch_pending"),
    ]
    assert first.next_cursor is not None

    inserted_after_anchor = replace(views[0], batch_id=CaptureBatchId("cbatch_new"))
    writer.publish(inserted_after_anchor)
    second = repository.recent_capture_batches(query, first.next_cursor)
    assert [view.batch_id for view in second.items] == [
        BATCH_PEER_FAILED,
        CaptureBatchId("cbatch_excessive_skew"),
    ]
    failed = repository.capture_batch(BATCH_PEER_FAILED)
    assert failed.attempts[1].observed_start_utc_ns == 225
    assert failed.observed_start_skew_ns == 22
    with pytest.raises(DashboardNotFound):
        repository.capture_batch(CaptureBatchId("cbatch_absent"))
    with pytest.raises(InvalidCursor):
        repository.recent_capture_batches(
            CaptureBatchTimeRangeQuery(UtcNs(0), UtcNs(401)), first.next_cursor
        )


@pytest.mark.integration
def test_batch_projection_roles_are_directional_and_source_independent(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT has_table_privilege(
                       'leo_dashboard', 'dashboard_capture_batch_projection', 'SELECT'),
                   has_table_privilege(
                       'leo_dashboard', 'dashboard_capture_attempt_projection', 'SELECT'),
                   has_table_privilege(
                       'leo_dashboard', 'dashboard_capture_batch_projection', 'INSERT'),
                   has_table_privilege(
                       'leo_capture', 'dashboard_capture_batch_projection', 'SELECT'),
                   has_table_privilege(
                       'leo_analysis', 'dashboard_capture_attempt_projection', 'SELECT'),
                   has_function_privilege(
                       'leo_capture', 'publish_dashboard_capture_batch(jsonb)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_analysis', 'publish_dashboard_capture_batch(jsonb)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard', 'publish_dashboard_capture_batch(jsonb)', 'EXECUTE')
                   ,has_function_privilege(
                       'leo_capture',
                       'resolve_dashboard_capture_batches_for_recording(text)',
                       'EXECUTE')
                   ,has_function_privilege(
                       'leo_analysis',
                       'resolve_dashboard_capture_batches_for_recording(text)',
                       'EXECUTE')
                   ,has_function_privilege(
                       'leo_dashboard',
                       'resolve_dashboard_capture_batches_for_recording(text)',
                       'EXECUTE')
            """
        ).fetchone()
    assert row == (
        True,
        True,
        False,
        False,
        False,
        True,
        True,
        False,
        False,
        True,
        False,
    )


@pytest.mark.integration
def test_analysis_projection_updates_every_matching_batch_and_replay_converges(
    postgres_dsn: str,
) -> None:
    manifest = recording_manifest(9)
    recording = published_recording(manifest)
    bundle, feature_ref = feature_bundle_and_ref(
        recording.recording_object, manifest, 9
    )
    command = FeatureProjectionCommand(bundle, feature_ref, recording)
    ready = capture_batches()[0].view
    pending_attempt = replace(
        ready.attempts[0],
        recording_id=manifest.recording_id,
        analysis_state=DashboardAnalysisState.PENDING,
        analysis_result_available=False,
    )
    first_batch = replace(ready, attempts=(pending_attempt, ready.attempts[1]))
    second_batch = replace(
        first_batch, batch_id=CaptureBatchId("cbatch_same_recording_second")
    )
    connect = _role_connect(postgres_dsn, "leo_analysis")
    batch_writer = PostgresCaptureBatchProjectionWriter(connect)
    batch_writer.publish(first_batch)
    batch_writer.publish(second_batch)

    class Delegate:
        def __init__(self) -> None:
            self.calls = 0

        def project_features(self, received: FeatureProjectionCommand):
            assert received == command
            self.calls += 1
            return ProjectionReceipt((91,))

    delegate = Delegate()
    writer = PostgresBatchAwareAnalysisProjectionWriter(
        connect, cast(AnalysisProjectionWriter, delegate)
    )
    assert writer.project_features(command) == ProjectionReceipt((91,))
    assert writer.project_features(command) == ProjectionReceipt((91,))
    assert delegate.calls == 2

    reader = PostgresCaptureBatchDashboardRepository(
        _role_connect(postgres_dsn, "leo_dashboard")
    )
    for batch_id in (first_batch.batch_id, second_batch.batch_id):
        projected = reader.capture_batch(batch_id)
        matching = [
            attempt
            for attempt in projected.attempts
            if attempt.recording_id == manifest.recording_id
        ]
        assert len(matching) == 1
        assert matching[0].analysis_state is DashboardAnalysisState.COMPLETE
        assert matching[0].analysis_result_available
        assert projected.observed_start_skew_ns == first_batch.observed_start_skew_ns
        assert (
            projected.paired_analysis_eligibility
            is first_batch.paired_analysis_eligibility
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_capture_batch_projection"
        ).fetchone() == (4,)
