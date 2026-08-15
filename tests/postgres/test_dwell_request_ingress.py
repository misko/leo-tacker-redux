from __future__ import annotations

import time
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.dwell_postgres import (
    DwellRequestConflictError,
    DwellRequestIntegrityError,
    DwellRequestSourceError,
    PostgresDwellRequestIngress,
    PostgresDwellRequestQueue,
)
from leo_flow.analysis.recording import DurableFeatureSetRepository
from leo_flow.contracts.capture import CapturePlanRef
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    PlanId,
    RadioId,
    SchemaRef,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dwell import DwellRequest, ScanResultRef
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.jobs.ports import StaleLeaseError
from tests.postgres.test_feature_sets import _publish_recording, _repository


def _connect_as(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return connect


def _request(
    postgres_dsn: str,
    root,
    *,
    issued_utc_ns: int | None = None,
    expires_utc_ns: int | None = None,
) -> DwellRequest:
    analysis_request, bundle = _publish_recording(postgres_dsn)
    features: DurableFeatureSetRepository = _repository(
        postgres_dsn, root / "feature-cas"
    )
    feature_ref = features.publish(
        analysis_request,
        bundle,
        idempotency_key="dwell-source-feature",
    )
    issued = time.time_ns() if issued_utc_ns is None else issued_utc_ns
    expires = issued + 30_000_000_000 if expires_utc_ns is None else expires_utc_ns
    evidence = (
        LabelEvidenceRef(
            SchemaRef(LabelEvidenceRef.SCHEMA_ID),
            "evidence_dwell_ingress",
            EvidenceKind.TLE_WEAK_ASSOCIATION,
            ArtifactRef(
                "artifact_dwell_ingress",
                Digest.sha256(b"dwell-ingress-evidence"),
                SchemaRef("org.leo-flow.dwell-ingress-evidence"),
            ),
            "producer_scan_analysis",
            UtcNs(max(0, issued - 1)),
        ),
    )
    source = ScanResultRef(
        SchemaRef(ScanResultRef.SCHEMA_ID),
        "scanresult_dwell_ingress",
        analysis_request.recording_id,
        analysis_request.recording_object_ref.identity_digest(),
        feature_ref,
        StationId("station_ingress"),
        RadioId("radio_ingress"),
        UtcNs(max(0, issued - 1)),
        1_825_000_000,
        1_000_000,
        800_000,
        evidence,
    )
    return DwellRequest(
        SchemaRef(DwellRequest.SCHEMA_ID),
        "dwell_ingress_one",
        source,
        source.station_id,
        source.radio_id,
        UtcNs(issued),
        UtcNs(expires),
        source.center_frequency_hz,
        source.sample_rate_hz,
        source.bandwidth_hz,
        1_000_000_000,
        1_000_000,
        "candidate_followup",
        evidence,
        "dwell:ingress:one",
    )


@pytest.mark.integration
def test_analysis_publishes_exact_catalog_lineage_and_capture_completes(
    postgres_dsn: str, tmp_path
) -> None:
    request = _request(postgres_dsn, tmp_path)
    ingress = PostgresDwellRequestIngress(_connect_as(postgres_dsn, "leo_analysis"))
    first = ingress.publish(request)
    assert ingress.publish(request) == first

    queue = PostgresDwellRequestQueue(
        _connect_as(postgres_dsn, "leo_capture"),
        token_factory=lambda: "lease_dwell_one",
    )
    assert queue.claim(StationId("station_other"), request.radio_id, 5) is None
    lease = queue.claim(request.station_id, request.radio_id, 5)
    assert lease is not None
    assert lease.request == request
    assert lease.request_digest == first.request_digest
    plan = CapturePlanRef(
        PlanId(f"plan_{request.request_id}"), Digest.sha256(b"durable-plan")
    )
    queue.complete(lease, plan)

    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT j.state, j.result_ref ->> 'artifact_id',
                   d.source_recording_id, d.source_feature_set_id
              FROM job AS j
              JOIN dwell_request_ingress AS d ON d.job_id = j.job_id
            """
        ).fetchone()
    assert row == (
        "succeeded",
        str(plan.plan_id),
        str(request.source.recording_id),
        str(request.source.feature_set_ref.feature_set_id),
    )


@pytest.mark.integration
def test_idempotency_conflict_rolls_back_extra_job(postgres_dsn: str, tmp_path) -> None:
    request = _request(postgres_dsn, tmp_path)
    ingress = PostgresDwellRequestIngress(_connect_as(postgres_dsn, "leo_analysis"))
    ingress.publish(request)
    changed = replace(
        request,
        request_id="dwell_ingress_changed",
        reason_code="different_reason",
    )

    with pytest.raises(DwellRequestConflictError):
        ingress.publish(changed)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dwell_request_ingress"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM job").fetchone() == (1,)


@pytest.mark.integration
def test_publication_rejects_substituted_feature_object_metadata(
    postgres_dsn: str, tmp_path
) -> None:
    request = _request(postgres_dsn, tmp_path)
    feature = request.source.feature_set_ref
    substituted = replace(
        feature,
        bundle_ref=replace(feature.bundle_ref, locator="cas:substituted"),
    )
    changed_source = replace(request.source, feature_set_ref=substituted)
    changed = replace(request, source=changed_source)

    with pytest.raises(DwellRequestSourceError, match="exact and authoritative"):
        PostgresDwellRequestIngress(_connect_as(postgres_dsn, "leo_analysis")).publish(
            changed
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dwell_request_ingress"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job").fetchone() == (0,)


@pytest.mark.integration
def test_expired_lease_restarts_with_generation_fence_and_retry(
    postgres_dsn: str, tmp_path
) -> None:
    request = _request(postgres_dsn, tmp_path)
    PostgresDwellRequestIngress(_connect_as(postgres_dsn, "leo_analysis")).publish(
        request
    )
    tokens = iter(("lease_first", "lease_restarted", "lease_early", "lease_retry"))
    queue = PostgresDwellRequestQueue(
        _connect_as(postgres_dsn, "leo_capture"),
        token_factory=lambda: next(tokens),
    )
    stale = queue.claim(request.station_id, request.radio_id, 0.02)
    assert stale is not None
    time.sleep(0.04)
    restarted = queue.claim(request.station_id, request.radio_id, 5)
    assert restarted is not None
    assert restarted.attempt == 2
    assert restarted.lease_generation == stale.lease_generation + 1
    with pytest.raises(StaleLeaseError):
        queue.complete(
            stale,
            CapturePlanRef(
                PlanId(f"plan_{request.request_id}"), Digest.sha256(b"stale")
            ),
        )

    retry_at = UtcNs(time.time_ns() + 30_000_000)
    queue.fail(restarted, "capture_transient", retry_at)
    assert queue.claim(request.station_id, request.radio_id, 5) is None
    time.sleep(0.05)
    retry = queue.claim(request.station_id, request.radio_id, 5)
    assert retry is not None and retry.attempt == 3


@pytest.mark.integration
def test_expired_request_is_parked_without_exposure(
    postgres_dsn: str, tmp_path
) -> None:
    now = time.time_ns()
    request = _request(
        postgres_dsn,
        tmp_path,
        issued_utc_ns=now - 2_000_000_000,
        expires_utc_ns=now - 1_000_000_000,
    )
    published = PostgresDwellRequestIngress(
        _connect_as(postgres_dsn, "leo_analysis")
    ).publish(request)
    queue = PostgresDwellRequestQueue(_connect_as(postgres_dsn, "leo_capture"))

    assert queue.claim(request.station_id, request.radio_id, 5) is None
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state, park_reason FROM job WHERE job_id = %s",
            (str(published.job_id),),
        ).fetchone() == ("parked", "request_expired")


@pytest.mark.integration
def test_unregistered_or_corrupt_payload_is_never_returned_as_valid(
    postgres_dsn: str, tmp_path
) -> None:
    request = _request(postgres_dsn, tmp_path)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO job(job_id, job_type, payload_schema_id,
                            payload_schema_version, payload, state,
                            available_at_utc)
            VALUES ('job_dwell_unregistered', 'dwell_capture',
                    'org.leo-flow.dwell-request', '0.1', '{}'::jsonb,
                    'ready', clock_timestamp())
            """
        )
    queue = PostgresDwellRequestQueue(_connect_as(postgres_dsn, "leo_capture"))
    assert queue.claim(request.station_id, request.radio_id, 5) is None

    published = PostgresDwellRequestIngress(
        _connect_as(postgres_dsn, "leo_analysis")
    ).publish(request)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE job
               SET payload = jsonb_set(payload, '{reason_code}', '"tampered"')
             WHERE job_id = %s
            """,
            (str(published.job_id),),
        )
    with pytest.raises(DwellRequestIntegrityError, match="digest"):
        queue.claim(request.station_id, request.radio_id, 5)
