from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.campaign_online_analysis_postgres import (
    PostgresCampaignAnalysisScopeRegistrarV1,
    PostgresCampaignConcurrentAnalysisGateV1,
)
from leo_flow.adapters.campaign_scoped_claims_postgres import (
    PostgresCampaignScopedJobClaimsV1,
)
from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    SchemaRef,
)
from leo_flow.contracts.deferred_analysis import DeferredAnalysisWindowV1
from leo_flow.jobs.contracts import JobPayload, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository


def _connect(dsn: str, role: str | None = None):
    def connect():
        connection = psycopg.connect(dsn, row_factory=dict_row)
        if role is not None:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _window(postgres_dsn: str) -> DeferredAnalysisWindowV1:
    recordings = tuple(RecordingId(f"rec_online_{index:02d}") for index in range(72))
    features = tuple(JobId(f"job_online_feature_{index:02d}") for index in range(72))
    waterfalls = tuple(
        JobId(f"job_online_waterfall_{index:02d}") for index in range(72)
    )
    suites = tuple(JobId(f"job_online_suite_{index:02d}") for index in range(72))
    repository = PostgresJobLeaseRepository(_connect(postgres_dsn))
    for job_type, identities in (
        (JobType.RECORDING_ANALYSIS, features),
        (JobType.WATERFALL_ANALYSIS, waterfalls),
        (JobType.STARLINK_SUITE_ANALYSIS, suites),
    ):
        for recording_id, job_id in zip(recordings, identities, strict=True):
            repository.enqueue(
                job_id,
                job_type,
                JobPayload.create(
                    SchemaRef("test.online-analysis"),
                    {"recording_id": str(recording_id)},
                ),
            )
    with psycopg.connect(postgres_dsn) as connection:
        for index in range(36):
            batch_id = f"cbatch_online_{index:02d}"
            sequence = connection.execute(
                "INSERT INTO dashboard_capture_batch_projection("
                "schema_id,schema_version,batch_id,capture_revision,mode,"
                "coordination_claim,requested_start_utc_ns,requested_start_skew_ns,"
                "observed_start_skew_ns,maximum_observed_start_skew_ns,"
                "paired_analysis_eligibility,semantic_view) "
                "VALUES('org.leo-flow.dashboard.capture-batch','0.1',%s,2,"
                "'coordinated','measured_software_coordination',%s,0,0,100000000,"
                "'eligible','{}') RETURNING projection_sequence",
                (batch_id, index),
            ).fetchone()[0]
            for position in range(2):
                recording_id = recordings[index * 2 + position]
                connection.execute(
                    "INSERT INTO dashboard_capture_attempt_projection("
                    "projection_sequence,attempt_position,attempt_id,radio_id,"
                    "plan_id,requested_start_utc_ns,capture_state,"
                    "observed_start_utc_ns,recording_id,analysis_state,"
                    "analysis_result_available) VALUES(%s,%s,%s,%s,%s,%s,"
                    "'succeeded',%s,%s,'pending',false)",
                    (
                        sequence,
                        position,
                        f"cattempt_online_{index:02d}_{position}",
                        f"radio_online_{position}",
                        f"plan_online_{index:02d}_{position}",
                        index,
                        index,
                        str(recording_id),
                    ),
                )
    return DeferredAnalysisWindowV1(
        Digest.sha256(b"online campaign"),
        0,
        tuple(CaptureBatchId(f"cbatch_online_{index:02d}") for index in range(36)),
        recordings,
        tuple(Digest.sha256(str(value).encode()) for value in recordings),
        features,
        waterfalls,
        suites,
    )


@pytest.mark.integration
def test_capture_allows_only_registered_same_campaign_leases(
    postgres_dsn: str,
) -> None:
    window = _window(postgres_dsn)
    PostgresCampaignAnalysisScopeRegistrarV1(
        _connect(postgres_dsn, "leo_analysis")
    ).register(window)
    lease = PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis")
    ).claim(
        window.feature_job_ids,
        JobType.RECORDING_ANALYSIS,
        "online-worker",
        30.0,
    )
    assert lease is not None

    assert PostgresCampaignConcurrentAnalysisGateV1(
        postgres_dsn, window.definition_digest
    ).ready()
    assert not PostgresCampaignConcurrentAnalysisGateV1(
        postgres_dsn, Digest.sha256(b"foreign campaign")
    ).ready()


@pytest.mark.integration
def test_unregistered_foreign_lease_keeps_capture_fail_closed(
    postgres_dsn: str,
) -> None:
    window = _window(postgres_dsn)
    PostgresCampaignAnalysisScopeRegistrarV1(
        _connect(postgres_dsn, "leo_analysis")
    ).register(window)
    outsider = JobId("job_online_foreign")
    repository = PostgresJobLeaseRepository(_connect(postgres_dsn))
    repository.enqueue(
        outsider,
        JobType.RECORDING_ANALYSIS,
        JobPayload.create(
            SchemaRef("test.online-analysis"), {"recording_id": "rec_foreign"}
        ),
    )
    assert PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis")
    ).claim((outsider,), JobType.RECORDING_ANALYSIS, "foreign", 30.0)

    assert not PostgresCampaignConcurrentAnalysisGateV1(
        postgres_dsn, window.definition_digest
    ).ready()


@pytest.mark.integration
def test_scope_registration_rejects_a_latest_inflight_batch(
    postgres_dsn: str,
) -> None:
    window = _window(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        sequence = connection.execute(
            "INSERT INTO dashboard_capture_batch_projection("
            "schema_id,schema_version,batch_id,capture_revision,mode,"
            "coordination_claim,requested_start_utc_ns,requested_start_skew_ns,"
            "observed_start_skew_ns,maximum_observed_start_skew_ns,"
            "paired_analysis_eligibility,semantic_view) "
            "VALUES('org.leo-flow.dashboard.capture-batch','0.1',%s,0,"
            "'coordinated','measured_software_coordination',0,0,NULL,100000000,"
            "'pending','{}') RETURNING projection_sequence",
            (str(window.batch_ids[0]),),
        ).fetchone()[0]
        for position in range(2):
            connection.execute(
                "INSERT INTO dashboard_capture_attempt_projection("
                "projection_sequence,attempt_position,attempt_id,radio_id,"
                "plan_id,requested_start_utc_ns,capture_state,analysis_state,"
                "analysis_result_available) VALUES(%s,%s,%s,%s,%s,0,"
                "'pending','unavailable',false)",
                (
                    sequence,
                    position,
                    f"cattempt_inflight_{position}",
                    f"radio_inflight_{position}",
                    f"plan_inflight_{position}",
                ),
            )

    with pytest.raises(psycopg.errors.CheckViolation):
        PostgresCampaignAnalysisScopeRegistrarV1(
            _connect(postgres_dsn, "leo_analysis")
        ).register(window)
