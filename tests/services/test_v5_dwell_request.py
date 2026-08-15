from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.application import (
    DurableDwellRequestGate,
    DwellRequestGate,
    DwellRequestRejected,
    DwellSafetyPolicy,
)
from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.fake_radio import FakePairedRadio, Refill
from leo_flow.capture.plan_repository import (
    CapturePlanConflictError,
    SQLiteCapturePlanRepository,
)
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureSetId,
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dwell import DwellRequest, ScanResultRef
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.deployments.v5_dwell_request import (
    DwellCaptureScheduleError,
    OneShotDwellCaptureScheduler,
)
from leo_flow.deployments.v5_dwell_request_e2e import (
    LIVE_DURATION_NS,
    LIVE_REFILL_COUNT,
    LIVE_SAMPLE_COUNT,
    LIVE_SAMPLE_RATE_HZ,
    live_request,
)
from testkit import FakeClock
from tests.capture._helpers import FakeCleaner, FakePublisher, FakeRecordingWriter, ci16

STATION = StationId("station_dwell")
RADIO = RadioId("radio_dwell")
RECEIVERS = (ReceiverChainId("rx_dwell_a"), ReceiverChainId("rx_dwell_b"))


def request() -> DwellRequest:
    evidence = LabelEvidenceRef(
        SchemaRef(LabelEvidenceRef.SCHEMA_ID),
        "evidence_dwell_scheduler",
        EvidenceKind.OPERATOR_NOTE,
        ArtifactRef(
            "artifact_dwell_scheduler",
            Digest.sha256(b"evidence"),
            SchemaRef("org.leo-flow.test-evidence"),
        ),
        "producer_test",
        UtcNs(100),
    )
    feature_bytes = b"feature"
    source = ScanResultRef(
        SchemaRef(ScanResultRef.SCHEMA_ID),
        "scanresult_dwell_scheduler",
        RecordingId("rec_dwell_source"),
        Digest.sha256(b"recording"),
        FeatureSetRef(
            FeatureSetId("fset_dwell_source"),
            AnalysisRunId("arun_dwell_source"),
            ObjectRef(
                Digest.sha256(feature_bytes),
                len(feature_bytes),
                "application/json",
                "feature-set-bundle-v0.1",
                "memory:feature",
            ),
        ),
        STATION,
        RADIO,
        UtcNs(100),
        1_825_000_000,
        1_000_000,
        800_000,
        (evidence,),
    )
    return DwellRequest(
        SchemaRef(DwellRequest.SCHEMA_ID),
        "dwell_scheduler",
        source,
        STATION,
        RADIO,
        UtcNs(200),
        UtcNs(1_000_000_200),
        source.center_frequency_hz,
        source.sample_rate_hz,
        source.bandwidth_hz,
        4_000,
        4,
        "candidate_requires_dwell",
        source.evidence_refs,
        "dwell:scheduler",
    )


def policy() -> DwellSafetyPolicy:
    return DwellSafetyPolicy(
        STATION,
        RADIO,
        RECEIVERS,
        GainSetting(GainMode.AGC),
        1_700_000_000,
        2_000_000_000,
        2_000_000,
        1_500_000,
        5_000_000_000,
        5_000_000,
    )


class RadioProvider:
    def __init__(self) -> None:
        self.opens = 0

    def open(self):
        self.opens += 1
        return FakePairedRadio(
            RADIO,
            RECEIVERS,
            {SegmentId("seg_dwell_scheduler"): (Refill(ci16(4)),)},
        )


def scheduler(tmp_path, publisher: FakePublisher, provider: RadioProvider):
    database = tmp_path / "capture.sqlite3"
    spool = SQLiteLocalSpool(database, tmp_path / "recordings")
    plans = SQLiteCapturePlanRepository(database)
    gate = DurableDwellRequestGate(DwellRequestGate(policy()), plans, plans)

    cleaner = FakeCleaner()
    return OneShotDwellCaptureScheduler(
        gate,
        provider,
        PlanCaptureEngine(
            CaptureIdentity(
                STATION,
                "serial-test",
                "fake-clock",
                HardwareSnapshotId("hw_dwell"),
                "dwell-scheduler-test",
            ),
            clock=FakeClock(),
        ),
        FakeRecordingWriter(),
        spool,
        PublicationReconciler(spool, publisher, cleaner),
    )


def test_restart_replay_returns_same_receipt_without_reopening_radio(tmp_path) -> None:
    publisher = FakePublisher()
    first_provider = RadioProvider()
    first = scheduler(tmp_path, publisher, first_provider).run(request(), UtcNs(300))
    assert first.captured_now is True
    assert first.spool_state is SpoolState.CLEANED
    assert first_provider.opens == 1

    restarted_provider = RadioProvider()
    replay = scheduler(tmp_path, publisher, restarted_provider).run(
        request(), UtcNs(400)
    )
    assert replay.recording_id == first.recording_id
    assert replay.plan_digest == first.plan_digest
    assert replay.captured_now is False
    assert restarted_provider.opens == 0


def test_conflict_and_expiry_are_rejected_before_radio_open(tmp_path) -> None:
    publisher = FakePublisher()
    provider = RadioProvider()
    composed = scheduler(tmp_path, publisher, provider)
    original = request()
    composed.run(original, UtcNs(300))

    with pytest.raises(CapturePlanConflictError, match="another plan"):
        scheduler(tmp_path, publisher, provider).run(
            replace(original, reason_code="changed_reason"), UtcNs(400)
        )
    with pytest.raises(DwellRequestRejected, match="currently valid"):
        scheduler(tmp_path, publisher, provider).run(
            replace(
                original,
                request_id="dwell_expired",
                idempotency_key="dwell:expired",
            ),
            original.expires_utc_ns,
        )
    assert provider.opens == 1


def test_publication_retry_uses_durable_bytes_without_recapture(tmp_path) -> None:
    failing = FakePublisher(failures_remaining=1)
    first_provider = RadioProvider()
    with pytest.raises(DwellCaptureScheduleError, match="publication"):
        scheduler(tmp_path, failing, first_provider).run(request(), UtcNs(300))
    assert first_provider.opens == 1

    restarted_provider = RadioProvider()
    receipt = scheduler(tmp_path, failing, restarted_provider).run(
        request(), UtcNs(400)
    )
    assert receipt.captured_now is False
    assert receipt.spool_state is SpoolState.CLEANED
    assert restarted_provider.opens == 0


def test_live_fixture_is_exact_block_aligned_and_contract_bounded() -> None:
    candidate = live_request(1_800_000_000_000_000_000)
    assert candidate.sample_count == LIVE_SAMPLE_COUNT
    assert candidate.sample_count == LIVE_REFILL_COUNT * 262_144
    assert candidate.duration_ns == LIVE_DURATION_NS
    assert (
        candidate.sample_count * 1_000_000_000
        == candidate.duration_ns * LIVE_SAMPLE_RATE_HZ
    )
    plan = DwellRequestGate(
        DwellSafetyPolicy(
            candidate.station_id,
            candidate.radio_id,
            RECEIVERS,
            GainSetting(GainMode.AGC),
            1_700_000_000,
            2_000_000_000,
            2_000_000,
            2_000_000,
            3_000_000_000,
            LIVE_SAMPLE_COUNT,
        )
    ).accept(candidate, UtcNs(candidate.issued_utc_ns + 1))
    segment = plan.activities[0].segments[0]
    assert segment.sample_count == LIVE_SAMPLE_COUNT
    assert segment.hardware_controls == ()
    assert dict(segment.tags)["tx"] == "prohibited"
