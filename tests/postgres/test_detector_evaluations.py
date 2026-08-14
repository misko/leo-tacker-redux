from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.evaluation_dashboard_postgres import PostgresEvaluationDashboard
from leo_flow.adapters.evaluation_postgres_catalog import (
    EvaluationConflictError,
    PostgresEvaluationCatalog,
    connection_factory,
)
from leo_flow.analysis.dataset.evaluation_persistence import (
    DurableDetectorEvaluationRepository,
)
from leo_flow.contracts.core import Digest, EvaluationRunId
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.dataset_analysis.test_evaluation_persistence import evaluation_report


def _dataset(postgres_dsn: str) -> None:
    report = evaluation_report()
    bundle = Digest.sha256(b"dataset-bundle")
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
            VALUES ('sha256', %s, 14, 'application/json', 'dataset-snapshot-bundle-v0.1', 'fixture://dataset')
            """,
            (bundle.value,),
        )
        connection.execute(
            """
            INSERT INTO dataset_snapshot
                (snapshot_id, feature_membership_digest_algorithm, feature_membership_digest_value,
                 snapshot_digest_algorithm, snapshot_digest_value,
                 bundle_digest_algorithm, bundle_digest_value,
                 evaluated_method_id, selection_spec, selection_cutoff_utc_ns,
                 promoted, promotion_warnings, member_count, idempotency_key)
            VALUES (%s, 'sha256', %s, 'sha256', %s, 'sha256', %s,
                    'energy@1', 'fixture', 1, false, '["fixture"]', 1, 'dataset:evaluation-fixture')
            """,
            (
                report.dataset_snapshot_id,
                report.feature_membership_digest.value,
                report.dataset_snapshot_digest.value,
                bundle.value,
            ),
        )


@pytest.mark.integration
def test_atomic_report_publication_retry_and_dashboard_projection(
    postgres_dsn: str, tmp_path
) -> None:
    _dataset(postgres_dsn)
    connect = connection_factory(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"), PostgresEvaluationCatalog(connect)
    )
    report = evaluation_report()
    first = repository.publish(
        EvaluationRunId("erun_pg"), report, idempotency_key="evaluation:pg"
    )
    second = repository.publish(
        EvaluationRunId("erun_pg"), report, idempotency_key="evaluation:pg"
    )
    assert first == second
    with repository.open(first) as opened:
        assert opened.report == report

    dashboard = PostgresEvaluationDashboard(connect).detector_evaluation(
        str(first.evaluation_id)
    )
    assert dashboard.ref == first
    assert dashboard.method_count == 1
    assert dashboard.union_window_count == 4
    assert dashboard.methods[0].method_id == "energy@1"
    assert dashboard.methods[0].true_positive == 1
    assert dashboard.ref.report_object.locator.startswith("cas:sha256:")
    with psycopg.connect(postgres_dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM detector_evaluation_report"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM detector_evaluation_method_summary"
            ).fetchone()[0]
            == 3
        )


@pytest.mark.integration
def test_run_and_idempotency_conflicts_are_atomic(postgres_dsn: str, tmp_path) -> None:
    _dataset(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(connection_factory(postgres_dsn)),
    )
    report = evaluation_report()
    repository.publish(
        EvaluationRunId("erun_conflict"), report, idempotency_key="evaluation:one"
    )
    changed = replace(report, warnings=("different",))
    with pytest.raises(EvaluationConflictError):
        repository.publish(
            EvaluationRunId("erun_conflict"), changed, idempotency_key="evaluation:two"
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM detector_evaluation_report"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM detector_evaluation_method_summary"
            ).fetchone()[0]
            == 3
        )


@pytest.mark.integration
def test_runtime_roles_enforce_append_only_and_dashboard_read_only(
    postgres_dsn: str, tmp_path
) -> None:
    _dataset(postgres_dsn)

    def analysis_connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute("SET ROLE leo_analysis")
        return connection

    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(analysis_connect),
    )
    ref = repository.publish(
        EvaluationRunId("erun_role"),
        evaluation_report(),
        idempotency_key="evaluation:role",
    )

    def dashboard_connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute("SET ROLE leo_dashboard")
        return connection

    view = PostgresEvaluationDashboard(dashboard_connect).detector_evaluation(
        str(ref.run_id)
    )
    assert view.ref == ref
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE detector_evaluation_report SET method_count = 9")


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
def test_method_summary_rejects_non_finite_thresholds(
    postgres_dsn: str, tmp_path, invalid: str
) -> None:
    _dataset(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(connection_factory(postgres_dsn)),
    )
    ref = repository.publish(
        EvaluationRunId("erun_finite"),
        evaluation_report(),
        idempotency_key="evaluation:finite",
    )
    with (
        psycopg.connect(postgres_dsn) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(
            """
            UPDATE detector_evaluation_method_summary
            SET threshold = %s::double precision
            WHERE evaluation_id = %s
            """,
            (invalid, str(ref.evaluation_id)),
        )


@pytest.mark.integration
def test_direct_analysis_role_cannot_commit_an_incomplete_summary(
    postgres_dsn: str, tmp_path
) -> None:
    _dataset(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(connection_factory(postgres_dsn)),
    )
    published = repository.publish(
        EvaluationRunId("erun_complete"),
        evaluation_report(),
        idempotency_key="evaluation:complete",
    )
    digest = Digest.sha256(b"incomplete-report")
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="incomplete"),
        psycopg.connect(postgres_dsn) as connection,
    ):
        connection.execute("SET ROLE leo_analysis")
        connection.execute(
            """
                INSERT INTO object_blob
                    (digest_algorithm, digest_value, byte_count, media_type,
                     format_id, locator)
                VALUES ('sha256', %s, 17, 'application/json',
                        'detector-evaluation-report-v0.1', 'fixture://incomplete')
                """,
            (digest.value,),
        )
        connection.execute(
            """
                INSERT INTO detector_evaluation_report
                    (evaluation_id, run_id, dataset_snapshot_id,
                     dataset_snapshot_digest_algorithm, dataset_snapshot_digest_value,
                     feature_membership_digest_algorithm, feature_membership_digest_value,
                     threshold_rule_id, threshold_rule_digest_algorithm,
                     threshold_rule_digest_value, calibration_dataset_id,
                     calibration_split, report_digest_algorithm, report_digest_value,
                     method_count, union_window_count, warnings, idempotency_key)
                SELECT %s, 'erun_incomplete_direct', dataset_snapshot_id,
                       dataset_snapshot_digest_algorithm, dataset_snapshot_digest_value,
                       feature_membership_digest_algorithm, feature_membership_digest_value,
                       threshold_rule_id, threshold_rule_digest_algorithm,
                       threshold_rule_digest_value, calibration_dataset_id,
                       calibration_split, 'sha256', %s, 1, union_window_count,
                       warnings, 'evaluation:incomplete-direct'
                FROM detector_evaluation_report WHERE evaluation_id = %s
                """,
            (
                f"eval_{digest.value}",
                digest.value,
                str(published.evaluation_id),
            ),
        )


def _corrupt_summary(postgres_dsn: str, statement: str, evaluation_id: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(statement, (evaluation_id,))
        connection.execute("SET session_replication_role = origin")


@pytest.mark.integration
def test_retry_and_dashboard_reject_an_incomplete_normalized_projection(
    postgres_dsn: str, tmp_path
) -> None:
    _dataset(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(connection_factory(postgres_dsn)),
    )
    report = evaluation_report()
    ref = repository.publish(
        EvaluationRunId("erun_corrupt"),
        report,
        idempotency_key="evaluation:corrupt",
    )
    _corrupt_summary(
        postgres_dsn,
        "DELETE FROM detector_evaluation_method_summary WHERE evaluation_id = %s AND split = 'validation'",
        str(ref.evaluation_id),
    )
    with pytest.raises(EvaluationConflictError, match="summaries differ"):
        repository.publish(
            EvaluationRunId("erun_corrupt"),
            report,
            idempotency_key="evaluation:corrupt",
        )
    with pytest.raises(RuntimeError, match="incomplete"):
        PostgresEvaluationDashboard(
            connection_factory(postgres_dsn)
        ).detector_evaluation(str(ref.evaluation_id))


@pytest.mark.integration
def test_retry_rejects_changed_normalized_values(postgres_dsn: str, tmp_path) -> None:
    _dataset(postgres_dsn)
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"),
        PostgresEvaluationCatalog(connection_factory(postgres_dsn)),
    )
    report = evaluation_report()
    ref = repository.publish(
        EvaluationRunId("erun_changed"),
        report,
        idempotency_key="evaluation:changed",
    )
    _corrupt_summary(
        postgres_dsn,
        "UPDATE detector_evaluation_method_summary SET firing_count = firing_count + 1 WHERE evaluation_id = %s AND split = 'train'",
        str(ref.evaluation_id),
    )
    with pytest.raises(EvaluationConflictError, match="summaries differ"):
        repository.publish(
            EvaluationRunId("erun_changed"),
            report,
            idempotency_key="evaluation:changed",
        )
