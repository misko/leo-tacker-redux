from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.capture_analysis_drain_postgres import (
    PostgresCaptureAnalysisDrainGate,
)
from leo_flow.adapters.dashboard_recording_postgres import (
    PostgresRecordingDashboardRepository,
    PostgresRecordingStarlinkProjectionWriter,
)
from leo_flow.adapters.starlink_analysis_postgres import (
    AtomicPostgresStarlinkCommitterV0_1,
)
from leo_flow.adapters.starlink_postgres_catalog import PostgresStarlinkCatalogV0_1
from leo_flow.adapters.starlink_projection_postgres import (
    PostgresStarlinkProjectionWorkRepositoryV0_1,
)
from leo_flow.analysis.recording.starlink import (
    KnownCodePilotSearchConfigV0_1,
    KnownCodePilotSearchV0_1,
    known_code_pilot_algorithm_ref_v0_1,
    known_code_pilot_config_ref_v0_1,
)
from leo_flow.analysis.recording.starlink_persistence import DurableStarlinkStoreV0_1
from leo_flow.application.starlink_projection_work import (
    StarlinkDashboardProjectionWorkerV0_1,
)
from leo_flow.contracts.core import JobId, SchemaRef
from leo_flow.contracts.starlink import StarlinkPilotAnalysisBundleV0_1
from leo_flow.contracts.starlink_pipeline import (
    StarlinkPilotAnalysisRequestV0_1,
    StarlinkStreamSelectionV0_1,
)
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.starlink_analysis import (
    PreparedStarlinkAnalysisV0_1,
    starlink_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.postgres.test_feature_projection_work import _capture_gate_login
from tests.postgres.test_feature_sets import _publish_recording
from tests.recording_analysis.fakes import SegmentFixture, execution_context, make_view
from tests.recording_analysis.test_starlink_pipeline import _templates


def _connect(postgres_dsn: str, role: str | None = None):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _claimed(postgres_dsn: str):
    feature_request, _ = _publish_recording(postgres_dsn)
    recording = feature_request.recording_object_ref
    templates = _templates()
    values = [0j] * 82
    for frame in range(5):
        start = 2 + frame * 16
        for index, sample in enumerate(templates.exact_samples):
            values[start + index] = sample
    raw = b"".join(
        struct.pack("<hhhh", round(sample.real), round(sample.imag), 0, 0)
        for sample in values
    )
    view, _ = make_view(
        SegmentFixture(raw, 12_000), recording_id=recording.recording_id
    )
    config = KnownCodePilotSearchConfigV0_1((0, 1, 2, 3), (0.0,))
    algorithm_ref = known_code_pilot_algorithm_ref_v0_1()
    config_ref = known_code_pilot_config_ref_v0_1(config)
    selection = StarlinkStreamSelectionV0_1(
        view.manifest.segments[0].segment_id,
        view.manifest.segments[0].requested.receiver_chain_ids[0],
        templates.edge,
        templates.exact_ref,
        templates.conditioned_control_ref,
        len(values),
    )
    request = StarlinkPilotAnalysisRequestV0_1(
        SchemaRef(StarlinkPilotAnalysisRequestV0_1.SCHEMA_ID),
        recording.recording_id,
        recording,
        algorithm_ref,
        config_ref,
        (selection,),
        SchemaRef(StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID),
    )
    candidate_bundle = KnownCodePilotSearchV0_1(
        config, execution_context()
    ).analyze_receiver(
        values,
        recording_id=recording.recording_id,
        recording_identity_digest=recording.identity_digest(),
        segment_id=selection.segment_id,
        receiver_chain_id=selection.receiver_chain_id,
        templates=templates,
    )
    jobs = PostgresJobLeaseRepository(_connect(postgres_dsn))
    job_id = JobId("job_starlink_candidate_atomic")
    jobs.enqueue(job_id, JobType.STARLINK_ANALYSIS, starlink_analysis_payload(request))
    lease = jobs.claim((JobType.STARLINK_ANALYSIS,), "starlink-worker", 30.0)
    assert lease is not None
    return jobs, lease, PreparedStarlinkAnalysisV0_1(request, candidate_bundle)


@pytest.mark.integration
def test_starlink_catalog_outbox_projection_and_dashboard_are_exact_and_fenced(
    postgres_dsn: str, tmp_path: Path
) -> None:
    jobs, lease, prepared = _claimed(postgres_dsn)
    cas = FileSystemBlobStore(tmp_path / "cas")
    analysis_connect = _connect(postgres_dsn, "leo_analysis")
    committer = AtomicPostgresStarlinkCommitterV0_1(cas, analysis_connect)
    result = committer.commit_starlink(lease, prepared)
    assert jobs.snapshot(lease.job_id).result_ref == result
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        assert not PostgresCaptureAnalysisDrainGate(capture_dsn).ready()
    with pytest.raises(StaleLeaseError):
        committer.commit_starlink(lease, prepared)

    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        source = connection.execute(
            "SELECT * FROM recording_starlink_candidate"
        ).fetchone()
        work = connection.execute("SELECT * FROM starlink_projection_work").fetchone()
        assert source is not None and source["candidate_count"] == 1
        assert work is not None and work["state"] == "ready"
        connection.execute(
            """INSERT INTO dashboard_recording_detail_projection(
                   recording_id,semantic_view) VALUES (%s,'{}'::jsonb)""",
            (str(prepared.request.recording_id),),
        )

    catalog = PostgresStarlinkCatalogV0_1(analysis_connect)
    store = DurableStarlinkStoreV0_1(cas, catalog)
    projection_work = PostgresStarlinkProjectionWorkRepositoryV0_1(analysis_connect)
    writer = PostgresRecordingStarlinkProjectionWriter(analysis_connect)
    projection = StarlinkDashboardProjectionWorkerV0_1(
        projection_work,
        store,
        writer,
        worker_id="starlink-projector",
        lease_ttl_s=30.0,
    )
    projection_lease = projection_work.claim("starlink-projector", 30.0)
    assert projection_lease is not None
    with (
        store.open(projection_lease.product_ref) as durable,
        pytest.raises(psycopg.Error, match="lease is not current"),
    ):
        writer.project_candidates(
            durable.bundle(),
            projection_lease.product_ref,
            replace(projection_lease, lease_token="stale-token"),
        )
    with store.open(projection_lease.product_ref) as durable:
        first_sequence = writer.project_candidates(
            durable.bundle(), projection_lease.product_ref, projection_lease
        )
        assert (
            writer.project_candidates(
                durable.bundle(), projection_lease.product_ref, projection_lease
            )
            == first_sequence
        )
    projection.execute(projection_lease)
    assert not projection.process_one_work()
    with _capture_gate_login(postgres_dsn, member=True) as capture_dsn:
        assert PostgresCaptureAnalysisDrainGate(capture_dsn).ready()

    dashboard = PostgresRecordingDashboardRepository(
        _connect(postgres_dsn, "leo_dashboard")
    )
    view = dashboard.recording_starlink_decision(prepared.request.recording_id)
    assert view.decision.calibrated_detection_count is None
    assert view.decision.search_candidate_count == 1
    assert len(view.candidates) == 1

    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        assert (
            connection.execute("SELECT state FROM starlink_projection_work").fetchone()[
                "state"
            ]
            == "succeeded"
        )
        privileges = connection.execute(
            """SELECT
              has_table_privilege(
                'leo_dashboard','recording_starlink_candidate','SELECT'
              ) AS dashboard_can_read_catalog,
              has_table_privilege(
                'leo_dashboard','starlink_projection_work','SELECT'
              ) AS dashboard_can_read_work,
              has_table_privilege(
                'leo_dashboard','dashboard_recording_starlink_projection','SELECT'
              ) AS dashboard_can_read_projection,
              has_function_privilege(
                'leo_dashboard',
                'publish_dashboard_recording_starlink(jsonb,text,text,bigint)',
                'EXECUTE'
              ) AS dashboard_can_publish"""
        ).fetchone()
        functions = connection.execute(
            """SELECT p.proname,
                      pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                      p.proconfig,
                      has_function_privilege(
                        'public',p.oid,'EXECUTE'
                      ) AS public_can_execute,
                      has_function_privilege(
                        'leo_analysis',p.oid,'EXECUTE'
                      ) AS analysis_can_execute,
                      has_function_privilege(
                        'leo_dashboard',p.oid,'EXECUTE'
                      ) AS dashboard_can_execute
                 FROM pg_catalog.pg_proc AS p
                 JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
                WHERE n.nspname='public' AND p.proname IN (
                  'publish_recording_starlink_candidate',
                  'publish_starlink_projection_work',
                  'claim_starlink_projection_work',
                  'complete_starlink_projection_work',
                  'retry_starlink_projection_work',
                  'park_starlink_projection_work',
                  'publish_dashboard_recording_starlink',
                  'read_starlink_analysis_receipt')
                ORDER BY p.proname"""
        ).fetchall()
    assert privileges == {
        "dashboard_can_read_catalog": False,
        "dashboard_can_read_work": False,
        "dashboard_can_read_projection": True,
        "dashboard_can_publish": False,
    }
    assert len(functions) == 8
    assert all(
        row["owner"] == "leo_routine_owner"
        and row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        and row["public_can_execute"] is False
        and row["analysis_can_execute"] is True
        and row["dashboard_can_execute"] is False
        for row in functions
    )
