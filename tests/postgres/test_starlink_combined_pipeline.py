from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.starlink_pilot_constellation_postgres import (
    PostgresStarlinkPilotConstellationCatalogV0_1,
)
from leo_flow.adapters.starlink_suite_postgres import (
    AtomicPostgresCombinedStarlinkSuiteCommitterV0_2,
)
from leo_flow.adapters.starlink_surrogate_null_postgres import (
    PostgresStarlinkSurrogateNullCatalogV0_1,
)
from leo_flow.adapters.starlink_temporal_pilot_postgres import (
    PostgresStarlinkTemporalPilotCatalogV0_1,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteV0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_suite_recording import (
    ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_recording import (
    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_recording import (
    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
)
from leo_flow.contracts.core import JobId
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.starlink_suite_analysis import starlink_suite_analysis_payload
from leo_flow.services.starlink_suite_surrogate_analysis import (
    CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
)
from leo_flow.services.starlink_surrogate_null_analysis import (
    StarlinkSurrogateNullAnalysisPreparerV0_1,
)
from leo_flow.services.starlink_temporal_pilot_analysis import (
    StarlinkTemporalPilotAnalysisPreparerV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from tests.recording_analysis.fakes import execution_context
from tests.recording_analysis.test_starlink_surrogate_null_persistence import (
    _fixture,
    _Reader,
)


def _connect(postgres_dsn: str, role: str | None = None):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


@pytest.mark.integration
def test_existing_suite_job_atomically_publishes_suite_null_qam_and_completion(
    postgres_dsn: str, tmp_path: Path
) -> None:
    view, recording, request, _, config = _fixture()
    PostgresRecordingCatalog(_connect(postgres_dsn, "leo_capture")).publish(
        recording, idempotency_key="recording:combined-starlink"
    )
    reader = _Reader(view)
    suite = ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
        StarlinkDetectorSuiteV0_2(config, execution_context())
    )
    preparer = CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
        reader,
        suite,
        (
            (
                request.config_ref,
                StarlinkSurrogateNullAnalysisPreparerV0_1(
                    reader,
                    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
                        config, execution_context()
                    ),
                    starlink_search_grid_v0_1(config),
                ),
            ),
        ),
        StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(), execution_context()
        ),
        (
            (
                request.config_ref,
                StarlinkTemporalPilotAnalysisPreparerV0_1(
                    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1(
                        config, execution_context()
                    ),
                    starlink_search_grid_v0_1(config),
                ),
            ),
        ),
    )
    jobs = PostgresJobLeaseRepository(_connect(postgres_dsn))
    job_id = JobId("job_starlink_suite_null_qam")
    jobs.enqueue(
        job_id,
        JobType.STARLINK_SUITE_ANALYSIS,
        starlink_suite_analysis_payload(request),
    )
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "combined-worker", 30.0)
    assert lease is not None
    prepared = preparer.prepare(lease)
    assert reader.open_count == 1
    assert prepared.pilot_constellation is not None
    assert prepared.temporal_pilot is not None

    blobs = FileSystemBlobStore(tmp_path / "cas")
    analysis_connect = _connect(postgres_dsn, "leo_analysis")
    committer = AtomicPostgresCombinedStarlinkSuiteCommitterV0_2(
        blobs, analysis_connect
    )
    result = committer.commit_starlink_suite(lease, prepared)

    assert result.artifact_id == prepared.bundle.analysis_id
    assert jobs.snapshot(job_id).result_ref == result
    assert (
        PostgresStarlinkSurrogateNullCatalogV0_1(
            analysis_connect
        ).latest_starlink_surrogate_null(recording.recording_id)
        is not None
    )
    assert (
        PostgresStarlinkPilotConstellationCatalogV0_1(
            analysis_connect
        ).latest_starlink_pilot_constellation(recording.recording_id)
        is not None
    )
    assert (
        PostgresStarlinkTemporalPilotCatalogV0_1(
            analysis_connect
        ).latest_starlink_temporal_pilot(recording.recording_id)
        is not None
    )
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT (SELECT count(*) FROM recording_starlink_detector_suite) AS suites,"
            "(SELECT count(*) FROM recording_starlink_surrogate_null) AS nulls,"
            "(SELECT count(*) FROM recording_starlink_pilot_constellation) AS qam,"
            "(SELECT count(*) FROM recording_starlink_temporal_pilot) AS temporal,"
            "(SELECT count(*) FROM starlink_detector_suite_projection_work) AS work"
        ).fetchone() == {
            "suites": 1,
            "nulls": 1,
            "qam": 1,
            "temporal": 1,
            "work": 1,
        }
    with pytest.raises(StaleLeaseError):
        committer.commit_starlink_suite(lease, prepared)
