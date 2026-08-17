from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.adapters.dashboard_recording_postgres import (
    PostgresRecordingStarlinkSuiteProjectionWriterV0_2,
)
from leo_flow.adapters.starlink_suite_postgres import (
    AtomicPostgresStarlinkSuiteCommitterV0_2,
    PostgresStarlinkSuiteCatalogV0_2,
    PostgresStarlinkSuiteProjectionWorkRepositoryV0_2,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    DurableStarlinkSuiteStoreV0_2,
)
from leo_flow.application.starlink_suite_projection_work import (
    StarlinkSuiteDashboardProjectionWorkerV0_2,
)
from leo_flow.contracts.core import JobId, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.starlink_suite_analysis import (
    PreparedStarlinkSuiteAnalysisV0_2,
    starlink_suite_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_station.analysis_v1 import (
    STARLINK_SUITE_ALGORITHM_REF,
    starlink_suite_profile_v0_2,
)
from tests.postgres.test_feature_sets import _publish_recording


def _connect(postgres_dsn: str, role: str | None = None):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


@pytest.mark.integration
def test_clipped_suite_is_durable_fenced_projected_and_explicit(
    postgres_dsn: str, tmp_path: Path
) -> None:
    feature_request, _ = _publish_recording(postgres_dsn)
    recording = feature_request.recording_object_ref
    request = StarlinkDetectorSuiteRequestV0_2(
        SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
        recording.recording_id,
        recording,
        STARLINK_SUITE_ALGORITHM_REF,
        starlink_suite_profile_v0_2(1_250_000.0).config_ref,
        (),
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
        "clipped-pilot-band",
    )
    analysis_token = canonical_digest(
        {"request": request, "state": StarlinkSuiteRecordingState.NOT_EVALUATED.value}
    ).value
    bundle = StarlinkDetectorSuiteRecordingBundleV0_2(
        request.requested_output_schema,
        f"slsuite_{analysis_token[:32]}",
        recording.recording_id,
        recording.identity_digest(),
        StarlinkSuiteRecordingState.NOT_EVALUATED,
        (),
        ("clipped-pilot-band",),
        None,
    )

    jobs = PostgresJobLeaseRepository(_connect(postgres_dsn))
    job_id = JobId("job_starlink_suite_clipped")
    jobs.enqueue(
        job_id,
        JobType.STARLINK_SUITE_ANALYSIS,
        starlink_suite_analysis_payload(request),
    )
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "suite-worker", 30.0)
    assert lease is not None
    blobs = FileSystemBlobStore(tmp_path / "cas")
    analysis_connect = _connect(postgres_dsn, "leo_analysis")
    committer = AtomicPostgresStarlinkSuiteCommitterV0_2(blobs, analysis_connect)
    result = committer.commit_starlink_suite(
        lease, PreparedStarlinkSuiteAnalysisV0_2(request, bundle)
    )
    assert jobs.snapshot(job_id).result_ref == result
    with pytest.raises(StaleLeaseError):
        committer.commit_starlink_suite(
            lease, PreparedStarlinkSuiteAnalysisV0_2(request, bundle)
        )

    catalog = PostgresStarlinkSuiteCatalogV0_2(analysis_connect)
    durable = DurableStarlinkSuiteStoreV0_2(blobs, catalog)
    work = PostgresStarlinkSuiteProjectionWorkRepositoryV0_2(analysis_connect)
    worker = StarlinkSuiteDashboardProjectionWorkerV0_2(
        work,
        durable,
        PostgresRecordingStarlinkSuiteProjectionWriterV0_2(analysis_connect),
        worker_id="suite-projector",
        lease_ttl_s=30.0,
    )
    assert worker.process_one_work()
    assert not worker.process_one_work()

    dashboard = PostgresDashboardRepository(_connect(postgres_dsn, "leo_dashboard"))
    view = dashboard.recording_starlink_suite(recording.recording_id)
    assert view.state is StarlinkSuiteRecordingState.NOT_EVALUATED
    assert view.analyzed_stream_count == 0
    assert view.method_count == 0
    assert view.methods == ()
    assert view.reason_codes == ("clipped-pilot-band",)
    assert view.calibrated_detection_count is None

    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        catalog_row = connection.execute(
            "SELECT result_state,suite_count,method_count "
            "FROM recording_starlink_detector_suite"
        ).fetchone()
        work_row = connection.execute(
            "SELECT state,attempt FROM starlink_detector_suite_projection_work"
        ).fetchone()
    assert catalog_row == {
        "result_state": "not_evaluated",
        "suite_count": 0,
        "method_count": 0,
    }
    assert work_row == {"state": "succeeded", "attempt": 1}
