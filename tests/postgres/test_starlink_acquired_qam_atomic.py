from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.starlink_acquired_constellation_postgres import (
    PostgresStarlinkAcquiredConstellationCatalogV0_3,
)
from leo_flow.adapters.starlink_suite_postgres import (
    AtomicPostgresCombinedStarlinkSuiteCommitterV0_3,
)
from leo_flow.analysis.recording.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationAnalyzerV0_3,
)
from leo_flow.analysis.recording.starlink_acquisition import (
    StarlinkAcquisitionConfigV0_3,
    StarlinkAcquisitionV0_3,
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
from leo_flow.contracts.core import JobId, ReceiverChainId
from leo_flow.jobs import JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.starlink_acquired_constellation_analysis import (
    CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3,
    StarlinkAcquiredDwellCompositionProfileV0_3,
)
from leo_flow.services.starlink_suite_analysis import starlink_suite_analysis_payload
from leo_flow.services.starlink_suite_surrogate_analysis import (
    CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
)
from leo_flow.services.starlink_surrogate_null_analysis import (
    StarlinkSurrogateNullAnalysisPreparerV0_1,
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
def test_v0_3_qam_is_published_in_same_fenced_suite_transaction(
    postgres_dsn: str, tmp_path: Path
) -> None:
    view, recording, request, _, suite_config = _fixture()
    PostgresRecordingCatalog(_connect(postgres_dsn, "leo_capture")).publish(
        recording, idempotency_key="recording:qam-v0.3"
    )
    reader = _Reader(view)
    legacy = CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
        reader,
        ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
            StarlinkDetectorSuiteV0_2(suite_config, execution_context())
        ),
        (
            (
                request.config_ref,
                StarlinkSurrogateNullAnalysisPreparerV0_1(
                    reader,
                    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
                        suite_config, execution_context()
                    ),
                    starlink_search_grid_v0_1(suite_config),
                ),
            ),
        ),
        StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(), execution_context()
        ),
    )
    acquisition_config = StarlinkAcquisitionConfigV0_3(
        "pg-qam-v0.3",
        retained_candidate_count=2,
        minimum_frame_support=1,
        maximum_probe_samples=5_000,
    )
    qam_config = StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=5_000)
    preparer = CombinedStarlinkSuiteDwellAnalysisJobPreparerV0_3(
        reader,
        legacy,
        (
            StarlinkAcquiredDwellCompositionProfileV0_3(
                request.config_ref,
                ReceiverChainId("rx_0"),
                StarlinkDetectorSuiteV0_2(suite_config, execution_context()),
                StarlinkAcquisitionV0_3(acquisition_config, execution_context()),
                StarlinkAcquiredPilotConstellationAnalyzerV0_3(
                    acquisition_config, qam_config, execution_context()
                ),
            ),
        ),
        window_sample_count=5_000,
        maximum_windows_per_stream=2,
    )
    jobs = PostgresJobLeaseRepository(_connect(postgres_dsn))
    job_id = JobId("job_starlink_qam_v03_atomic")
    jobs.enqueue(
        job_id,
        JobType.STARLINK_SUITE_ANALYSIS,
        starlink_suite_analysis_payload(request),
    )
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "qam-v03-worker", 60.0)
    assert lease is not None
    prepared = preparer.prepare(lease)
    assert prepared.acquired_constellation_v0_3 is not None
    assert len(prepared.acquired_constellation_v0_3.bundle.streams[0].windows) == 2
    connect = _connect(postgres_dsn, "leo_analysis")
    result = AtomicPostgresCombinedStarlinkSuiteCommitterV0_3(
        FileSystemBlobStore(tmp_path / "cas"), connect
    ).commit_starlink_suite(lease, prepared)
    assert jobs.snapshot(job_id).result_ref == result
    latest = PostgresStarlinkAcquiredConstellationCatalogV0_3(
        connect
    ).latest_starlink_acquired_constellation(recording.recording_id)
    assert latest is not None
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT stream_count,window_count,point_count,calibration_required FROM recording_starlink_acquired_constellation_v0_3"
        ).fetchone() == {
            "stream_count": 1,
            "window_count": 2,
            "point_count": 4_800,
            "calibration_required": True,
        }
