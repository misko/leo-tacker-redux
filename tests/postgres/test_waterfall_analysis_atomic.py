from __future__ import annotations

import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.waterfall_analysis_postgres import (
    AtomicPostgresWaterfallCommitterV0_1,
)
from leo_flow.adapters.waterfall_receipt_postgres import (
    PostgresWaterfallReceiptReaderV0_1,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    JobId,
    Provenance,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
    WaterfallProductId,
    WaterfallTileV0_1,
    WaterfallTimeBinV0_1,
)
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services.waterfall_analysis import (
    PreparedWaterfallAnalysisV0_1,
    waterfall_analysis_payload,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.postgres.test_feature_sets import _publish_recording


def _connect(postgres_dsn: str, *, role: bool = False):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        if role:
            connection.execute("SET ROLE leo_analysis")
        return connection

    return connect


def _claimed(postgres_dsn: str, *, ttl_s: float = 5.0):
    feature_request, _ = _publish_recording(postgres_dsn)
    recording = feature_request.recording_object_ref
    algorithm = ArtifactRef("waterfall-test-algorithm", Digest.sha256(b"algorithm"))
    config = ArtifactRef("waterfall-test-config", Digest.sha256(b"config"))
    request = WaterfallAnalysisRequestV0_1(
        SchemaRef(WaterfallAnalysisRequestV0_1.SCHEMA_ID),
        recording.recording_id,
        recording,
        algorithm,
        config,
        (),
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
    )
    bundle = WaterfallBundleV0_1(
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
        WaterfallProductId("waterfall_" + "1" * 32),
        AnalysisRunId("arun_" + "2" * 32),
        recording.recording_id,
        recording.identity_digest(),
        Provenance(
            "waterfall-test",
            "0.1",
            "commit",
            Digest.sha256(b"environment"),
            config.digest,
            (recording.identity_digest(),),
            (algorithm.digest,),
            UtcNs(100),
            UtcNs(101),
            "test-host",
        ),
        (
            WaterfallTileV0_1(
                SegmentId("seg_waterfall"),
                ReceiverChainId("rx_waterfall"),
                UtcNs(1_000),
                16,
                1_500_000_000.0,
                16_000.0,
                8,
                "counts-squared-per-bin",
                (-8_000.0, -4_000.0, 0.0, 4_000.0),
                (
                    WaterfallTimeBinV0_1(
                        0,
                        8,
                        UtcNs(251_000),
                        (-20.0, -10.0, 0.0, -10.0),
                    ),
                ),
            ),
        ),
    )
    jobs = PostgresJobLeaseRepository(_connect(postgres_dsn))
    job_id = JobId("job_waterfall_atomic")
    jobs.enqueue(
        job_id, JobType.WATERFALL_ANALYSIS, waterfall_analysis_payload(request)
    )
    lease = jobs.claim((JobType.WATERFALL_ANALYSIS,), "worker", ttl_s)
    assert lease is not None
    return jobs, lease, PreparedWaterfallAnalysisV0_1(request, bundle)


def _committer(postgres_dsn: str, root: Path, *, role: bool = False):
    return AtomicPostgresWaterfallCommitterV0_1(
        FileSystemBlobStore(root), _connect(postgres_dsn, role=role)
    )


@pytest.mark.integration
def test_waterfall_catalog_work_receipt_and_job_commit_atomically(
    postgres_dsn: str, tmp_path: Path
) -> None:
    _, lease, prepared = _claimed(postgres_dsn)
    result = _committer(postgres_dsn, tmp_path / "cas", role=True).commit_waterfall(
        lease, prepared
    )

    receipt = PostgresWaterfallReceiptReaderV0_1(
        _connect(postgres_dsn, role=True)
    ).read(lease.job_id)
    assert receipt is not None
    assert receipt.waterfall_ref.product_id == prepared.bundle.product_id
    assert receipt.waterfall_ref.bundle_ref.digest == result.digest
    assert (
        receipt.input_recording_digest
        == prepared.request.recording_object_ref.identity_digest()
    )
    assert receipt.work_state == "ready"
    assert receipt.projected_utc_ns is None
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM job WHERE job_id=%s", (str(lease.job_id),)
        ).fetchone() == ("succeeded",)
        assert connection.execute(
            "SELECT count(*) FROM recording_waterfall"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM waterfall_projection_work"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_stale_waterfall_lease_rolls_back_catalog_and_work(
    postgres_dsn: str, tmp_path: Path
) -> None:
    jobs, stale, prepared = _claimed(postgres_dsn, ttl_s=0.03)
    time.sleep(0.05)
    current = jobs.claim((JobType.WATERFALL_ANALYSIS,), "replacement", 5.0)
    assert current is not None

    with pytest.raises(StaleLeaseError):
        _committer(postgres_dsn, tmp_path / "cas").commit_waterfall(stale, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM recording_waterfall"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM waterfall_projection_work"
        ).fetchone() == (0,)


@pytest.mark.integration
def test_capture_gate_blocks_only_current_waterfall_leases(
    postgres_dsn: str, tmp_path: Path
) -> None:
    jobs, lease, prepared = _claimed(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT capture_analysis_inactive()").fetchone() == (
            False,
        )
    jobs.fail(
        lease.job_id,
        lease.lease_token,
        lease.lease_generation,
        "test-retry",
        UtcNs(0),
    )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT capture_analysis_inactive()").fetchone() == (
            True,
        )

    current = jobs.claim((JobType.WATERFALL_ANALYSIS,), "replacement", 5.0)
    assert current is not None
    _committer(postgres_dsn, tmp_path / "cas").commit_waterfall(current, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        claimed = connection.execute(
            "SELECT * FROM claim_waterfall_projection_work(%s, interval '1 hour')",
            ("projection-lease",),
        ).fetchone()
        assert claimed is not None
        assert connection.execute("SELECT capture_analysis_inactive()").fetchone() == (
            False,
        )
        connection.execute(
            "UPDATE waterfall_projection_work SET lease_expires_utc=clock_timestamp()-interval '1 second'"
        )
        assert connection.execute("SELECT capture_analysis_inactive()").fetchone() == (
            True,
        )
