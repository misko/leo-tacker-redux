from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest

from leo_flow.adapters.dashboard_capture_doppler_postgres import (
    PostgresCaptureDopplerSnapshotRepositoryV0_1,
)
from leo_flow.adapters.hardware_link_postgres import (
    PostgresRecordingHardwareLinkCatalog,
)
from leo_flow.adapters.hardware_postgres_catalog import (
    PostgresHardwareSnapshotCatalog,
    connection_factory,
)
from leo_flow.adapters.waterfall_doppler_postgres import (
    AtomicPostgresWaterfallDopplerCommitterV0_1,
)
from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    AdvancedBlindDopplerAnalyzerV0_1,
    PreparedTileDopplerV0_1,
    PreparedWaterfallDopplerV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureSnapshotQueryV0_1,
)
from leo_flow.contracts.hardware import RecordingHardwareLink
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
)
from leo_flow.hardware import DurableHardwareMetadataRepository
from leo_flow.services.waterfall_doppler_analysis import (
    PreparedCombinedWaterfallAnalysisV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.hardware.test_hardware_persistence import _snapshot
from tests.postgres.test_waterfall_analysis_atomic import _claimed, _connect
from tests.recording_analysis.test_waterfall_doppler_pipeline import _basic, _bundle


@pytest.mark.integration
def test_projection_is_private_and_interval_read_is_dashboard_only_and_bounded(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """
            SELECT
              has_table_privilege('leo_dashboard',
                'dashboard_capture_doppler_product_v0_1','SELECT'),
              has_table_privilege('leo_analysis',
                'dashboard_capture_doppler_candidate_v0_1','INSERT'),
              has_function_privilege('leo_analysis',
                'publish_dashboard_capture_doppler_product_v0_1(text,text,jsonb)',
                'EXECUTE'),
              has_function_privilege('leo_dashboard',
                'publish_dashboard_capture_doppler_product_v0_1(text,text,jsonb)',
                'EXECUTE'),
              has_function_privilege('leo_dashboard',
                'read_dashboard_capture_doppler_summaries_v0_1(bigint,bigint,integer)',
                'EXECUTE'),
              has_function_privilege('leo_analysis',
                'read_dashboard_capture_doppler_summaries_v0_1(bigint,bigint,integer)',
                'EXECUTE')
            """
        ).fetchone()
        definition = connection.execute(
            """SELECT pg_get_functiondef(
              'read_dashboard_capture_doppler_summaries_v0_1(bigint,bigint,integer)'
              ::regprocedure)"""
        ).fetchone()
    assert privileges == (False, False, True, False, True, False)
    assert definition is not None
    assert "LIMIT $3" in str(definition[0])
    assert "object_blob" not in str(definition[0])

    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            connection.execute(
                "SELECT * FROM read_dashboard_capture_doppler_summaries_v0_1(1,2,101)"
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("analysis_state", "expected"),
    (("pending", "pending"), ("failed", "failed"), ("complete", "not_analyzed")),
)
def test_snapshot_states_are_durable_and_never_inferred_as_no_candidate(
    postgres_dsn: str, analysis_state: str, expected: str
) -> None:
    _capture_row(postgres_dsn, f"rec_{expected}", analysis_state, "radio_v5")
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        rows = connection.execute(
            "SELECT summary_state FROM "
            "read_dashboard_capture_doppler_summaries_v0_1(1,1000,10)"
        ).fetchall()
    assert rows == [(expected,)]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("with_candidate", "expected"), ((True, "complete"), (False, "no_candidate"))
)
def test_atomic_committer_closes_complete_and_no_candidate_without_cas_reads(
    postgres_dsn: str, tmp_path, with_candidate: bool, expected: str
) -> None:
    recording, receiver_id = _commit_product(
        postgres_dsn, tmp_path / "cas", with_candidate=with_candidate
    )
    assert receiver_id == "rx_v5_0"
    _link_hardware(postgres_dsn, tmp_path / "hardware", recording)
    _capture_row(postgres_dsn, str(recording.recording_id), "complete", "radio_v5")

    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        rows = connection.execute(
            "SELECT summary_state,lnb_id,receiver_chain_id,candidate_id,model "
            "FROM read_dashboard_capture_doppler_summaries_v0_1(1,1000,10)"
        ).fetchall()
    if with_candidate:
        assert len(rows) == 1
        assert rows[0][0:3] == ("complete", "lnb-b", "rx_v5_0")
        assert rows[0][3] is not None
        assert rows[0][4] in {"constant", "linear", "quadratic"}
    else:
        assert rows == [("no_candidate", None, None, None, None)]


@pytest.mark.integration
def test_master_snapshot_adapter_reads_requested_pg_receipt_closure(
    postgres_dsn: str, tmp_path
) -> None:
    recording, _ = _commit_product(
        postgres_dsn, tmp_path / "cas", with_candidate=True
    )
    _link_hardware(postgres_dsn, tmp_path / "hardware", recording)
    _capture_row(postgres_dsn, str(recording.recording_id), "complete", "radio_v5")
    repository = PostgresCaptureDopplerSnapshotRepositoryV0_1(
        lambda: psycopg.connect(postgres_dsn, options="-c role=leo_dashboard")
    )

    result = repository.capture_doppler_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(1000), 10),
        (RecordingId(str(recording.recording_id)),),
    )

    summary = result[RecordingId(str(recording.recording_id))]
    assert summary.state.value == "complete"
    assert len(summary.candidates) == 1
    candidate = summary.candidates[0]
    assert (candidate.radio_id, candidate.lnb_id, candidate.receiver_chain_id) == (
        "radio_v5",
        "lnb-b",
        "rx_v5_0",
    )


def _commit_product(
    postgres_dsn: str, root, *, with_candidate: bool
) -> tuple[RecordingObjectRef, str]:
    _, lease, legacy = _claimed(postgres_dsn)
    recording_digest = legacy.request.recording_object_ref.identity_digest()
    waterfall = _bundle("rx_v5_0")
    waterfall = replace(
        waterfall,
        recording_id=legacy.request.recording_id,
        input_recording_identity_digest=recording_digest,
        provenance=replace(
            waterfall.provenance,
            input_digests=(recording_digest,),
            dependency_digests=(Digest.sha256(b"algorithm"),),
        ),
    )
    spectrogram, basic = _basic(waterfall)
    if not with_candidate:
        basic = replace(basic, candidates=(), reason_codes=("no-candidate",))
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    request = WaterfallAnalysisRequestV0_2(
        SchemaRef(WaterfallAnalysisRequestV0_2.SCHEMA_ID, V0_2),
        legacy.request.recording_id,
        legacy.request.recording_object_ref,
        ArtifactRef("waterfall-v0.2-test", Digest.sha256(b"algorithm")),
        ArtifactRef("waterfall-v0.2-config", Digest.sha256(b"config")),
        (),
        SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
    )
    prepared = PreparedCombinedWaterfallAnalysisV0_1(
        legacy.request,
        legacy.bundle,
        PreparedWaterfallDopplerV0_1(
            request,
            waterfall,
            (PreparedTileDopplerV0_1(spectrogram, basic, advanced),),
        ),
    )
    AtomicPostgresWaterfallDopplerCommitterV0_1(
        FileSystemBlobStore(root), _connect(postgres_dsn, role=True)
    ).commit_waterfall(lease, prepared)
    with psycopg.connect(postgres_dsn) as connection:
        receipt = connection.execute(
            "SELECT candidate_count,receiver_chain_id "
            "FROM dashboard_capture_doppler_product_v0_1"
        ).fetchone()
        rows = connection.execute(
            "SELECT count(*) FROM dashboard_capture_doppler_candidate_v0_1"
        ).fetchone()
    assert receipt is not None and rows is not None
    assert receipt[0] == rows[0] == len(basic.candidates)
    return legacy.request.recording_object_ref, str(receipt[1])


def _link_hardware(postgres_dsn: str, root, recording: RecordingObjectRef) -> None:
    hardware = DurableHardwareMetadataRepository(
        FileSystemBlobStore(root),
        PostgresHardwareSnapshotCatalog(connection_factory(postgres_dsn)),
    ).publish(_snapshot(), idempotency_key="doppler-summary-hardware")
    identity = {
        "recording_id": str(recording.recording_id),
        "recording_identity_digest": str(recording.identity_digest()),
        "hardware_snapshot_id": str(hardware.snapshot_id),
        "hardware_snapshot_digest": str(hardware.digest),
    }
    digest = canonical_digest(identity)
    link = RecordingHardwareLink(
        f"hwlink_{digest.value[:32]}",
        recording.recording_id,
        recording.identity_digest(),
        hardware,
        digest,
    )
    PostgresRecordingHardwareLinkCatalog(connection_factory(postgres_dsn)).publish(
        link, idempotency_key="doppler-summary-hardware-link"
    )


def _capture_row(
    postgres_dsn: str, recording_id: str, analysis_state: str, radio_id: str
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        sequence = connection.execute(
            """INSERT INTO dashboard_capture_batch_projection(
                   schema_id,schema_version,batch_id,capture_revision,mode,
                   coordination_claim,requested_start_utc_ns,
                   requested_start_skew_ns,observed_start_skew_ns,
                   maximum_observed_start_skew_ns,paired_analysis_eligibility,
                   semantic_view)
                 VALUES ('org.leo-flow.dashboard.capture-batch','0.1',
                   'cbatch_summary',2,'independent','none',100,1,1,NULL,
                   'eligible','{}') RETURNING projection_sequence"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO dashboard_capture_attempt_projection(
                   projection_sequence,attempt_position,attempt_id,radio_id,
                   plan_id,requested_start_utc_ns,capture_state,
                   observed_start_utc_ns,recording_id,failure_reason,
                   analysis_state,analysis_result_available)
                 VALUES (%s,0,'cattempt_summary',%s,'plan_summary',100,
                   'succeeded',101,%s,NULL,%s,%s),
                   (%s,1,'cattempt_summary_peer','radio_peer','plan_summary_peer',
                   101,'failed',NULL,NULL,'fixture-failure','unavailable',false)""",
            (
                sequence,
                radio_id,
                recording_id,
                analysis_state,
                analysis_state == "complete",
                sequence,
            ),
        )
