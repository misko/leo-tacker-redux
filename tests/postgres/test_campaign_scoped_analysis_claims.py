from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.campaign_scoped_claims_postgres import (
    PostgresCampaignScopedJobClaimsV1,
)
from leo_flow.contracts.core import JobId, SchemaRef
from leo_flow.jobs.contracts import JobPayload, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository


def _connect(postgres_dsn: str, role: str | None = None):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role is not None:
            connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _enqueue(postgres_dsn: str, *job_ids: JobId) -> None:
    repository = PostgresJobLeaseRepository(_connect(postgres_dsn))
    for job_id in job_ids:
        repository.enqueue(
            job_id,
            JobType.RECORDING_ANALYSIS,
            JobPayload.create(SchemaRef("test.campaign-scoped"), {"id": str(job_id)}),
        )


@pytest.mark.integration
def test_scoped_job_claim_never_leases_an_unlisted_identity(
    postgres_dsn: str,
) -> None:
    first = JobId("job_campaign_scope_first")
    second = JobId("job_campaign_scope_second")
    outsider = JobId("job_campaign_scope_outsider")
    _enqueue(postgres_dsn, first, second, outsider)
    claims = PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis"),
        token_factory=iter(("lease_scope_1", "lease_scope_2")).__next__,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = tuple(
            pool.map(
                lambda worker: claims.claim(
                    (first, second), JobType.RECORDING_ANALYSIS, worker, 30.0
                ),
                ("worker-a", "worker-b"),
            )
        )

    assert {lease.job_id for lease in leases if lease is not None} == {first, second}
    with psycopg.connect(postgres_dsn) as connection:
        state = connection.execute(
            "SELECT state FROM job WHERE job_id=%s", (str(outsider),)
        ).fetchone()
    assert state == ("ready",)


@pytest.mark.integration
def test_scoped_job_claim_recovers_only_an_expired_fenced_lease(
    postgres_dsn: str,
) -> None:
    job_id = JobId("job_campaign_scope_expiry")
    _enqueue(postgres_dsn, job_id)
    claims = PostgresCampaignScopedJobClaimsV1(
        _connect(postgres_dsn, "leo_analysis"),
        token_factory=iter(
            ("lease_expired", "lease_blocked", "lease_recovered")
        ).__next__,
    )
    expired = claims.claim((job_id,), JobType.RECORDING_ANALYSIS, "first", 0.03)
    assert expired is not None
    assert claims.claim((job_id,), JobType.RECORDING_ANALYSIS, "blocked", 1.0) is None
    time.sleep(0.05)
    recovered = claims.claim((job_id,), JobType.RECORDING_ANALYSIS, "second", 30.0)
    assert recovered is not None
    assert recovered.job_id == job_id
    assert recovered.lease_generation == expired.lease_generation + 1
    assert recovered.lease_token != expired.lease_token


@pytest.mark.integration
def test_scoped_job_claim_requires_the_exact_job_type(postgres_dsn: str) -> None:
    job_id = JobId("job_campaign_scope_type")
    _enqueue(postgres_dsn, job_id)
    claims = PostgresCampaignScopedJobClaimsV1(_connect(postgres_dsn, "leo_analysis"))

    assert claims.claim((job_id,), JobType.WATERFALL_ANALYSIS, "worker", 30.0) is None
    lease = claims.claim((job_id,), JobType.RECORDING_ANALYSIS, "worker", 30.0)

    assert lease is not None
    assert lease.job_type is JobType.RECORDING_ANALYSIS


@pytest.mark.integration
def test_campaign_claim_functions_are_analysis_only_and_strictly_bounded(
    postgres_dsn: str,
) -> None:
    signatures = (
        "claim_campaign_analysis_job(text[],text,text,interval)",
        "claim_campaign_feature_projection(text[],text,interval)",
        "claim_campaign_waterfall_projection(text[],text,interval)",
        "claim_campaign_starlink_suite_projection(text[],text,interval)",
        "read_campaign_analysis_lane_status(text,text[])",
    )
    with psycopg.connect(postgres_dsn) as connection:
        for signature in signatures:
            privileges = connection.execute(
                "SELECT has_function_privilege('leo_analysis',%s,'EXECUTE'),"
                "has_function_privilege('leo_capture',%s,'EXECUTE'),"
                "has_function_privilege('leo_dashboard',%s,'EXECUTE'),"
                "NOT EXISTS ("
                "SELECT 1 FROM pg_proc AS p "
                "CROSS JOIN LATERAL aclexplode("
                "coalesce(p.proacl,acldefault('f',p.proowner))) AS acl "
                "WHERE p.oid=to_regprocedure(%s) AND acl.grantee=0 "
                "AND acl.privilege_type='EXECUTE')",
                (signature, signature, signature, signature),
            ).fetchone()
            assert privileges == (True, False, False, True)
    connect = _connect(postgres_dsn, "leo_analysis")
    claims = PostgresCampaignScopedJobClaimsV1(connect)
    duplicate = JobId("job_campaign_duplicate")
    with pytest.raises(ValueError, match="unique exact"):
        claims.claim((duplicate, duplicate), JobType.RECORDING_ANALYSIS, "worker", 30.0)
