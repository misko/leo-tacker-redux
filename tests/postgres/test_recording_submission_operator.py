from __future__ import annotations

import psycopg
import pytest

from leo_flow.deployments.recording_submission_v1 import (
    analysis_connection_factory,
    submit_recording_analysis,
)
from leo_flow.jobs import JobState, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.recording_submission_operator import (
    ExactRecordingAnalysisSelection,
    RecordingSubmissionOperatorConfig,
)
from leo_flow.storage.postgres_catalog import (
    PostgresRecordingCatalog,
    connection_factory,
)
from tests.recording_analysis.test_feature_persistence import _fixture


class _Credential:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def resolve(self, name: str) -> str:
        assert name == "analysis-catalog-dsn"
        return self._dsn


@pytest.mark.integration
def test_operator_resolves_catalog_and_enqueues_one_durable_exact_job(
    postgres_dsn: str,
) -> None:
    request, _ = _fixture()
    catalog = PostgresRecordingCatalog(connection_factory(postgres_dsn))
    catalog.publish(request.recording_object_ref, idempotency_key="published-input")
    config = RecordingSubmissionOperatorConfig(
        ExactRecordingAnalysisSelection(
            request.recording_id,
            request.algorithm_ref,
            request.config_ref,
            request.dependency_refs,
            request.requested_output_schema,
        ),
        "analysis-catalog-dsn",
    )

    first = submit_recording_analysis(config, credentials=_Credential(postgres_dsn))
    second = submit_recording_analysis(config, credentials=_Credential(postgres_dsn))

    assert first == second
    assert first.request == request
    jobs = PostgresJobLeaseRepository(analysis_connection_factory(postgres_dsn))
    assert jobs.snapshot(first.job_id).state is JobState.READY
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT count(*), min(job_type), min(payload_schema_id)
            FROM job
            WHERE job_id = %s
            """,
            (str(first.job_id),),
        ).fetchone()
    assert row == (
        1,
        JobType.RECORDING_ANALYSIS.value,
        "org.leo-flow.recording-analysis-job",
    )


@pytest.mark.integration
def test_every_operator_connection_assumes_restricted_analysis_role(
    postgres_dsn: str,
) -> None:
    with analysis_connection_factory(postgres_dsn)() as connection:
        row = connection.execute("SELECT current_user, session_user").fetchone()
    assert row is not None
    assert row["current_user"] == "leo_analysis"
    assert row["session_user"] != row["current_user"]
