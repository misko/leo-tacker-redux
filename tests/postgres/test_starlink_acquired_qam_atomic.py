from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.adapters.starlink_acquired_constellation_postgres import (
    PostgresStarlinkAcquiredConstellationCatalogV0_3,
)
from leo_flow.adapters.starlink_suite_postgres import (
    AtomicPostgresCombinedStarlinkSuiteCommitterV0_3,
    PostgresStarlinkSuiteCatalogV0_2,
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
from leo_flow.contracts.core import JobId, ReceiverChainId, canonical_digest
from leo_flow.contracts.dashboard_qam_summary_receipt import (
    DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2,
    dashboard_qam_candidate_set_digest_v0_2,
)
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
    source = PostgresStarlinkSuiteCatalogV0_2(connect).latest_starlink_suite(
        recording.recording_id
    )
    assert source is not None
    assert source.projection.request_digest == canonical_digest(prepared.request)
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
        source_row = connection.execute(
            "SELECT to_jsonb(source)-'published_at_utc' AS payload "
            "FROM recording_starlink_acquired_constellation_v0_3 source"
        ).fetchone()
        receipt = connection.execute(
            "SELECT source_kind,analysis_id,recording_id,"
            "source_request_digest_value,source_product_digest_value,"
            "summary_config_digest_value,candidate_set_digest_value,"
            "terminal_outcome,candidate_count,candidate_only,calibration_required "
            "FROM dashboard_capture_qam_summary_receipt_v0_2"
        ).fetchone()
    assert source_row is not None
    payload = source_row["payload"]
    assert isinstance(payload, dict)
    assert receipt == {
        "source_kind": "acquired-v0.3",
        "analysis_id": prepared.acquired_constellation_v0_3.bundle.analysis_id,
        "recording_id": str(recording.recording_id),
        "source_request_digest_value": payload["request_digest_value"],
        "source_product_digest_value": payload["bundle_digest_value"],
        "summary_config_digest_value": DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.value,
        "candidate_set_digest_value": dashboard_qam_candidate_set_digest_v0_2(
            []
        ).value,
        "terminal_outcome": "no-candidate",
        "candidate_count": 0,
        "candidate_only": True,
        "calibration_required": True,
    }

    empty_digest = dashboard_qam_candidate_set_digest_v0_2([]).value
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        connection.execute("SET ROLE leo_analysis")
        assert connection.execute(
            "SELECT public.publish_dashboard_capture_qam_summary_receipt_v0_2(%s,%s,%s,%s,%s,%s) AS published",
            (
                "acquired-v0.3",
                payload["analysis_id"],
                DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.value,
                empty_digest,
                "no-candidate",
                Jsonb([]),
            ),
        ).fetchone() == {"published": True}

    with (
        pytest.raises(psycopg.errors.UniqueViolation),
        psycopg.connect(postgres_dsn, row_factory=dict_row) as connection,
    ):
        connection.execute("SET ROLE leo_analysis")
        connection.execute(
            "SELECT public.publish_dashboard_capture_qam_summary_receipt_v0_2(%s,%s,%s,%s,%s,%s)",
            (
                "acquired-v0.3",
                payload["analysis_id"],
                DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.value,
                "b" * 64,
                "no-candidate",
                Jsonb([]),
            ),
        )

    cloned_analysis_id = "slqam3rec_" + "e" * 32
    cloned_payload = {
        **payload,
        "analysis_id": cloned_analysis_id,
        "request_digest_value": "e" * 64,
        "idempotency_key": "recording:qam-v0.3:receipt-rollback",
    }
    with (
        pytest.raises(psycopg.errors.InvalidParameterValue),
        psycopg.connect(postgres_dsn, row_factory=dict_row) as connection,
    ):
        connection.execute("SET ROLE leo_analysis")
        assert connection.execute(
            "SELECT public.publish_recording_starlink_acquired_constellation_v0_3(%s) AS published",
            (Jsonb(cloned_payload),),
        ).fetchone() == {"published": True}
        connection.execute(
            "SELECT public.publish_dashboard_capture_qam_summary_receipt_v0_2(%s,%s,%s,%s,%s,%s)",
            (
                "acquired-v0.3",
                cloned_analysis_id,
                DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest.value,
                "not-a-digest",
                "no-candidate",
                Jsonb([]),
            ),
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording_starlink_acquired_constellation_v0_3 "
            "WHERE analysis_id=%s",
            (cloned_analysis_id,),
        ).fetchone() == (0,)
