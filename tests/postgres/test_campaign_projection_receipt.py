from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from leo_flow.adapters.campaign_analysis_receipt_postgres import (
    PostgresCampaignProjectionReceiptReader,
)
from leo_flow.contracts.core import JobId
from leo_flow.jobs.contracts import JobState
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from tests.postgres.test_feature_projection_work import (
    _connect_as,
    _publish,
    _repository,
    _worker,
)


@pytest.mark.integration
def test_exact_campaign_projection_receipt_converges_without_table_access(
    postgres_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path / "cas"
    lease, prepared = _publish(postgres_dsn, root)
    connect = lambda: _connect_as(postgres_dsn, "leo_analysis")
    reader = PostgresCampaignProjectionReceiptReader(connect)

    ready = reader.read(lease.job_id)
    assert ready is not None
    assert ready.source_job_id == lease.job_id
    assert ready.recording_id == prepared.request.recording_id
    assert ready.state == "ready"
    assert ready.projected_utc_ns is None
    assert ready.feature_ref.feature_set_id == prepared.bundle.feature_set_id
    job = PostgresJobLeaseRepository(connect).snapshot(lease.job_id)
    assert job.state is JobState.SUCCEEDED
    assert job.result_ref is not None
    assert job.result_ref.artifact_id == str(ready.feature_ref.feature_set_id)
    assert job.result_ref.digest == ready.feature_ref.bundle_ref.digest
    assert ready.job_result == job.result_ref

    worker = _worker(postgres_dsn, root, _repository(postgres_dsn, ["receipt-token"]))
    assert worker.process_one_work()
    succeeded = reader.read(lease.job_id)
    assert succeeded is not None
    assert succeeded.state == "succeeded"
    assert succeeded.projected_utc_ns is not None
    assert succeeded.feature_ref == ready.feature_ref

    with (
        psycopg.connect(postgres_dsn) as connection,
    ):
        privileges = connection.execute(
            """
            SELECT has_function_privilege(
                       'leo_analysis',
                       'read_feature_projection_receipt(text)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_capture',
                       'read_feature_projection_receipt(text)', 'EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard',
                       'read_feature_projection_receipt(text)', 'EXECUTE')
            """
        ).fetchone()
    assert privileges == (True, False, False)


@pytest.mark.integration
def test_campaign_projection_receipt_unknown_job_is_absent(
    postgres_dsn: str,
) -> None:
    reader = PostgresCampaignProjectionReceiptReader(
        lambda: _connect_as(postgres_dsn, "leo_analysis")
    )
    assert reader.read(JobId("job_unknown_campaign_receipt")) is None
