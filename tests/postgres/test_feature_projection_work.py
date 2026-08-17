from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from leo_flow.adapters.capture_analysis_drain_postgres import (
    PostgresCaptureAnalysisDrainGate,
)
from leo_flow.adapters.dashboard_batch_postgres import (
    PostgresBatchAwareAnalysisProjectionWriter,
    PostgresCaptureBatchDashboardRepository,
    PostgresCaptureBatchProjectionWriter,
)
from leo_flow.adapters.dashboard_projection_postgres import (
    PostgresAnalysisProjectionWriter,
)
from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
from leo_flow.adapters.feature_projection_work_postgres import (
    PostgresFeatureProjectionWorkRepository,
)
from leo_flow.adapters.recording_analysis_postgres import (
    AtomicPostgresRecordingAnalysisCommitter,
)
from leo_flow.analysis.recording import DurableFeatureSetRepository
from leo_flow.application.feature_projection_work import (
    FeatureProjectionWorker,
    StaleFeatureProjectionLeaseError,
)
from leo_flow.application.projection_writers import FeatureProjectionCommand
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardView,
    DashboardAnalysisState,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.dashboard._fixtures import capture_batches
from tests.postgres.test_recording_analysis_atomic import _claimed


@contextmanager
def _capture_gate_login(postgres_dsn: str, *, member: bool) -> Iterator[str]:
    login = f"capture_gate_{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(24)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(login), sql.Literal(password))
        )
        if member:
            connection.execute(
                sql.SQL("GRANT leo_capture TO {}").format(sql.Identifier(login))
            )
    try:
        yield make_conninfo(postgres_dsn, user=login, password=password)
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


def _connect_as(postgres_dsn: str, role: str):
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    connection.execute(f"SET ROLE {role}")
    return connection


def _publish(
    postgres_dsn: str,
    root: Path,
    *,
    zero_observations: bool = False,
):
    _, lease, prepared = _claimed(postgres_dsn)
    if zero_observations:
        prepared = replace(
            prepared,
            bundle=replace(prepared.bundle, observations=(), method_scores=()),
        )
    committer = AtomicPostgresRecordingAnalysisCommitter(
        FileSystemBlobStore(root),
        lambda: _connect_as(postgres_dsn, "leo_analysis"),
    )
    committer.commit(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO dashboard_recording_projection(
                recording_id, radio_id, started_utc_ns, finished_utc_ns,
                analysis_state, segment_count, recording_object_available)
            VALUES (%s, 'radio_projection_work', 1, 2, 'pending', 1, true)
            """,
            (str(prepared.request.recording_id),),
        )
    return lease, prepared


def _repository(postgres_dsn: str, tokens: list[str]):
    return PostgresFeatureProjectionWorkRepository(
        lambda: _connect_as(postgres_dsn, "leo_analysis"),
        token_factory=lambda: tokens.pop(0),
    )


def _worker(postgres_dsn: str, root: Path, repository):
    connect = lambda: _connect_as(postgres_dsn, "leo_analysis")
    features = DurableFeatureSetRepository(
        FileSystemBlobStore(root), PostgresFeatureSetCatalog(connect)
    )
    return FeatureProjectionWorker(
        repository,
        features,
        PostgresRecordingCatalog(connect),
        PostgresBatchAwareAnalysisProjectionWriter(
            connect, PostgresAnalysisProjectionWriter(connect)
        ),
        worker_id="projection-worker",
        lease_ttl_s=5,
        retry_delay_s=0.01,
    )


@pytest.mark.integration
def test_capture_drain_gate_tracks_latest_dashboard_and_projection_work(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _publish(postgres_dsn, tmp_path / "cas")
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisDrainGate(capture_dsn)
        assert not gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO dashboard_recording_projection(
                    recording_id, radio_id, started_utc_ns, finished_utc_ns,
                    analysis_state, segment_count, recording_object_available)
                SELECT recording_id, radio_id, started_utc_ns, finished_utc_ns,
                       'complete', segment_count, recording_object_available
                  FROM dashboard_recording_projection
                 ORDER BY projection_sequence DESC LIMIT 1
                """
            )
        assert not gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE feature_projection_work
                   SET state = 'succeeded', projected_at_utc = clock_timestamp()
                """
            )
        assert gate.ready()


@pytest.mark.integration
def test_capture_drain_gate_blocks_active_recording_job_and_has_exact_acl(
    postgres_dsn: str,
) -> None:
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        gate = PostgresCaptureAnalysisDrainGate(capture_dsn)
        with (
            psycopg.connect(capture_dsn, autocommit=True) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute("SELECT count(*) FROM feature_projection_work")
        assert gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                INSERT INTO job(
                    job_id, job_type, payload_schema_id, payload_schema_version,
                    payload, state, available_at_utc)
                VALUES ('job_drain_gate', 'recording_analysis', 'test', '0.1', '{}',
                        'ready', clock_timestamp())
                """
            )
        assert not gate.ready()
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE job SET state = 'parked', park_reason = 'operator_parked',
                               parked_at_utc = clock_timestamp()
                 WHERE job_id = 'job_drain_gate'
                """
            )
            privileges = connection.execute(
                """
                SELECT has_function_privilege(
                           'leo_capture', 'capture_analysis_drain_ready()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_analysis', 'capture_analysis_drain_ready()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_dashboard', 'capture_analysis_drain_ready()', 'EXECUTE'),
                       has_function_privilege(
                           'leo_maintenance', 'capture_analysis_drain_ready()', 'EXECUTE')
                """
            ).fetchone()
        assert gate.ready()
        assert privileges == (True, False, False, False)


@pytest.mark.integration
def test_capture_drain_gate_rejects_nonmember_login(postgres_dsn: str) -> None:
    with (
        _capture_gate_login(postgres_dsn, member=False) as nonmember_dsn,
        pytest.raises(RuntimeError, match="not a leo_capture role member"),
    ):
        PostgresCaptureAnalysisDrainGate(nonmember_dsn).ready()


@pytest.mark.integration
def test_batch_publication_failure_leaves_work_retryable_and_replay_converges(
    postgres_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path / "cas"
    _, prepared = _publish(postgres_dsn, root)
    connect = lambda: _connect_as(postgres_dsn, "leo_analysis")
    ready = capture_batches()[0].view
    batch = replace(
        ready,
        attempts=(
            replace(
                ready.attempts[0],
                recording_id=prepared.request.recording_id,
                analysis_state=DashboardAnalysisState.PENDING,
                analysis_result_available=False,
            ),
            ready.attempts[1],
        ),
    )
    PostgresCaptureBatchProjectionWriter(connect).publish(batch)

    class FailingPublisher:
        def publish(self, view: CaptureBatchDashboardView) -> int:
            del view
            raise RuntimeError("injected batch projection failure")

    repository = _repository(postgres_dsn, ["fplease_fail", "fplease_retry"])
    worker = FeatureProjectionWorker(
        repository,
        DurableFeatureSetRepository(
            FileSystemBlobStore(root), PostgresFeatureSetCatalog(connect)
        ),
        PostgresRecordingCatalog(connect),
        PostgresBatchAwareAnalysisProjectionWriter(
            connect,
            PostgresAnalysisProjectionWriter(connect),
            batch_publisher=FailingPublisher(),
        ),
        worker_id="projection-worker-failure",
        lease_ttl_s=5,
        retry_delay_s=0.01,
    )
    with pytest.raises(RuntimeError, match="injected batch projection failure"):
        worker.process_one_work()
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM feature_projection_work"
        ).fetchone() == ("failed",)
        assert connection.execute(
            """
            SELECT analysis_state FROM dashboard_recording_projection
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone() == ("complete",)

    time.sleep(0.02)
    assert _worker(postgres_dsn, root, repository).process_one_work()
    projected = PostgresCaptureBatchDashboardRepository(
        lambda: _connect_as(postgres_dsn, "leo_dashboard")
    ).capture_batch(batch.batch_id)
    assert projected.attempts[0].analysis_state is DashboardAnalysisState.COMPLETE
    assert projected.attempts[0].analysis_result_available
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM feature_projection_work"
        ).fetchone() == ("succeeded",)


@pytest.mark.integration
@pytest.mark.parametrize("zero_observations", [False, True])
def test_durable_worker_projects_and_completes_exact_work(
    postgres_dsn: str,
    tmp_path: Path,
    zero_observations: bool,
) -> None:
    root = tmp_path / "cas"
    _, prepared = _publish(postgres_dsn, root, zero_observations=zero_observations)
    repository = _repository(postgres_dsn, ["fplease_project"])
    assert _worker(postgres_dsn, root, repository).process_one_work()

    with psycopg.connect(postgres_dsn) as connection:
        work = connection.execute(
            """
            SELECT state, attempt, lease_generation, projected_at_utc IS NOT NULL
            FROM feature_projection_work
            """
        ).fetchone()
        feature_count = connection.execute(
            "SELECT count(*) FROM dashboard_feature_projection"
        ).fetchone()[0]
        analysis_state = connection.execute(
            """
            SELECT analysis_state FROM dashboard_recording_projection
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone()[0]
    assert work == ("succeeded", 1, 1, True)
    assert feature_count == len(prepared.bundle.observations)
    assert analysis_state == "complete"


@pytest.mark.integration
def test_expired_claim_replays_existing_projection_without_duplicates(
    postgres_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path / "cas"
    _, prepared = _publish(postgres_dsn, root)
    repository = _repository(postgres_dsn, ["fplease_abandoned", "fplease_replay"])
    abandoned = repository.claim("abandoned-worker", 0.03)
    assert abandoned is not None

    connect = lambda: _connect_as(postgres_dsn, "leo_analysis")
    features = DurableFeatureSetRepository(
        FileSystemBlobStore(root), PostgresFeatureSetCatalog(connect)
    )
    recording = PostgresRecordingCatalog(connect).get(prepared.request.recording_id)
    assert recording is not None
    with features.open(abandoned.work.feature_set_ref) as view:
        PostgresAnalysisProjectionWriter(connect).project_features(
            FeatureProjectionCommand(
                view.bundle(), abandoned.work.feature_set_ref, recording
            )
        )

    time.sleep(0.05)
    assert _worker(postgres_dsn, root, repository).process_one_work()
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dashboard_feature_projection"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT state, attempt, lease_generation FROM feature_projection_work"
        ).fetchone() == ("succeeded", 2, 2)


@pytest.mark.integration
def test_stale_projection_generation_cannot_complete_retry_or_park(
    postgres_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path / "cas"
    _publish(postgres_dsn, root)
    repository = _repository(postgres_dsn, ["fplease_stale", "fplease_current"])
    stale = repository.claim("first", 0.03)
    assert stale is not None
    time.sleep(0.05)
    current = repository.claim("second", 5)
    assert current is not None

    with pytest.raises(StaleFeatureProjectionLeaseError):
        repository.complete(
            stale.work.work_id, stale.lease_token, stale.lease_generation
        )
    with pytest.raises(StaleFeatureProjectionLeaseError):
        repository.retry(
            stale.work.work_id,
            stale.lease_token,
            stale.lease_generation,
            "stale_retry",
            1,
        )
    with pytest.raises(StaleFeatureProjectionLeaseError):
        repository.park(
            stale.work.work_id,
            stale.lease_token,
            stale.lease_generation,
            "stale_park",
        )
    repository.park(
        current.work.work_id,
        current.lease_token,
        current.lease_generation,
        "test_terminal",
    )


@pytest.mark.integration
def test_authority_mismatch_parks_without_dashboard_mutation(
    postgres_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path / "cas"
    _publish(postgres_dsn, root)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE feature_projection_work
               SET recording_digest_value = %s
            """,
            ("f" * 64,),
        )
    repository = _repository(postgres_dsn, ["fplease_mismatch"])
    assert _worker(postgres_dsn, root, repository).process_one_work()
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state, park_reason FROM feature_projection_work"
        ).fetchone() == ("parked", "projection_identity_mismatch")
        assert connection.execute(
            "SELECT count(*) FROM dashboard_feature_projection"
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT analysis_state FROM dashboard_recording_projection
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone() == ("pending",)
