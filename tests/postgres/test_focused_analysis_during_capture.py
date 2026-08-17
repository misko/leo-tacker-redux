from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.campaign_scoped_claims_postgres import (
    PostgresCampaignScopedJobClaimsV1,
)
from leo_flow.adapters.focused_analysis_postgres import (
    PostgresFocusedAnalysisPairScopeRegistrarV0_1,
    PostgresRegisteredAnalysisSafetyGateV3,
)
from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    SchemaRef,
)
from leo_flow.contracts.focused_analysis import FocusedAnalysisPairScopeV0_1
from leo_flow.jobs.contracts import JobPayload, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository


def _connect(dsn: str, role: str | None = None):  # type: ignore[no-untyped-def]
    def connect():  # type: ignore[no-untyped-def]
        connection = psycopg.connect(dsn, row_factory=dict_row)
        if role is not None:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _scope(postgres_dsn: str) -> FocusedAnalysisPairScopeV0_1:
    recordings = (RecordingId("rec_focused_a"), RecordingId("rec_focused_b"))
    feature = (JobId("job_focused_feature_a"), JobId("job_focused_feature_b"))
    waterfall = (
        JobId("job_focused_waterfall_a"),
        JobId("job_focused_waterfall_b"),
    )
    suites = (JobId("job_focused_suite_a"), JobId("job_focused_suite_b"))
    repository = PostgresJobLeaseRepository(_connect(postgres_dsn))
    for job_type, identities in (
        (JobType.RECORDING_ANALYSIS, feature),
        (JobType.WATERFALL_ANALYSIS, waterfall),
        (JobType.STARLINK_SUITE_ANALYSIS, suites),
    ):
        for recording_id, job_id in zip(recordings, identities, strict=True):
            repository.enqueue(
                job_id,
                job_type,
                JobPayload.create(
                    SchemaRef("test.focused-analysis"),
                    {"recording_id": str(recording_id)},
                ),
            )
    batch_id = CaptureBatchId("cbatch_focused_capture_safe")
    with psycopg.connect(postgres_dsn) as connection:
        sequence = connection.execute(
            "INSERT INTO dashboard_capture_batch_projection("
            "schema_id,schema_version,batch_id,capture_revision,mode,"
            "coordination_claim,requested_start_utc_ns,requested_start_skew_ns,"
            "observed_start_skew_ns,maximum_observed_start_skew_ns,"
            "paired_analysis_eligibility,semantic_view) "
            "VALUES('org.leo-flow.dashboard.capture-batch','0.1',%s,2,"
            "'coordinated','measured_software_coordination',0,0,0,100000000,"
            "'eligible','{}') RETURNING projection_sequence",
            (str(batch_id),),
        ).fetchone()[0]
        for position, recording_id in enumerate(recordings):
            connection.execute(
                "INSERT INTO dashboard_capture_attempt_projection("
                "projection_sequence,attempt_position,attempt_id,radio_id,"
                "plan_id,requested_start_utc_ns,capture_state,"
                "observed_start_utc_ns,recording_id,analysis_state,"
                "analysis_result_available) VALUES(%s,%s,%s,%s,%s,0,"
                "'succeeded',0,%s,'pending',false)",
                (
                    sequence,
                    position,
                    f"cattempt_focused_{position}",
                    f"radio_focused_{position}",
                    f"plan_focused_{position}",
                    str(recording_id),
                ),
            )
    return FocusedAnalysisPairScopeV0_1(
        Digest.sha256(b"focused capture definition"),
        batch_id,
        recordings,
        (Digest.sha256(b"recording a"), Digest.sha256(b"recording b")),
        feature,
        waterfall,
        suites,
    )


@pytest.mark.integration
def test_registered_focused_pair_lease_is_capture_safe(postgres_dsn: str) -> None:
    scope = _scope(postgres_dsn)
    registrar = PostgresFocusedAnalysisPairScopeRegistrarV0_1(
        _connect(postgres_dsn, "leo_analysis")
    )
    registrar.register(scope)
    registrar.register(scope)
    lease = PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis")
    ).claim(
        scope.feature_job_ids,
        JobType.RECORDING_ANALYSIS,
        "focused-worker",
        30.0,
    )
    assert lease is not None
    assert PostgresRegisteredAnalysisSafetyGateV3(
        postgres_dsn, Digest.sha256(b"next focused capture")
    ).ready()


@pytest.mark.integration
def test_unregistered_lease_still_closes_v3_capture_gate(postgres_dsn: str) -> None:
    scope = _scope(postgres_dsn)
    PostgresFocusedAnalysisPairScopeRegistrarV0_1(
        _connect(postgres_dsn, "leo_analysis")
    ).register(scope)
    outsider = JobId("job_focused_outsider")
    PostgresJobLeaseRepository(_connect(postgres_dsn)).enqueue(
        outsider,
        JobType.RECORDING_ANALYSIS,
        JobPayload.create(
            SchemaRef("test.focused-analysis"),
            {"recording_id": "rec_focused_outsider"},
        ),
    )
    assert PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis")
    ).claim((outsider,), JobType.RECORDING_ANALYSIS, "outsider", 30.0)
    assert not PostgresRegisteredAnalysisSafetyGateV3(
        postgres_dsn, Digest.sha256(b"next focused capture")
    ).ready()


@pytest.mark.integration
def test_focused_registration_rejects_nonterminal_latest_projection(
    postgres_dsn: str,
) -> None:
    scope = _scope(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            "UPDATE dashboard_capture_batch_projection "
            "SET capture_revision=0,paired_analysis_eligibility='pending',"
            "observed_start_skew_ns=NULL "
            "WHERE batch_id=%s",
            (str(scope.batch_id),),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        PostgresFocusedAnalysisPairScopeRegistrarV0_1(
            _connect(postgres_dsn, "leo_analysis")
        ).register(scope)
