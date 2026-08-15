"""Supervised local DwellRequest-to-capture-to-analysis V5 rehearsal."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DurableFeatureSetRepository,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.application import (
    DurableDwellRequestGate,
    DwellRequestGate,
    DwellSafetyPolicy,
)
from leo_flow.capture.clock import SystemCaptureClock
from leo_flow.capture.engine import PlanCaptureEngine
from leo_flow.capture.plan_repository import SQLiteCapturePlanRepository
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.dwell import DwellRequest, ScanResultRef
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.ports import RadioDevice
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.deployments.v5_canary import CaptureHostGuard, V5RadioProvider
from leo_flow.deployments.v5_dwell_e2e import (
    BLOCK_SAMPLES,
    CAPTURE_IDENTITY,
    EXPECTED_SERIAL,
    EXPECTED_URI,
    RADIO_ID,
    RECEIVER_CHAINS,
    object_integrity_evidence,
    require_empty_output_root,
    require_live_confirmation,
    sustained_continuity_evidence,
)
from leo_flow.deployments.v5_dwell_request import OneShotDwellCaptureScheduler
from leo_flow.deployments.v5_dwell_supervisor import (
    ClockAttestation,
    RetentionCapacityPolicy,
    SQLiteSupervisorState,
    TrustedCaptureClock,
    V5DwellSupervisor,
)
from leo_flow.deployments.v5_live_safety import verify_tx2_muted
from leo_flow.jobs import InMemoryJobLeaseRepository, JobState
from leo_flow.maintenance.capacity import (
    CapacityConfiguration,
    CapacityRoot,
    CapacityThresholds,
)
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    PreparedRecordingAnalysis,
    RecordingAnalysisJobPreparer,
)
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)

REPORT_SCHEMA = "org.leo-flow.v5-dwell-request-e2e-report/v1"
LIVE_SAMPLE_RATE_HZ = 2_000_000
LIVE_BANDWIDTH_HZ = 1_800_000
LIVE_CENTER_FREQUENCY_HZ = 1_825_000_000
LIVE_REFILL_COUNT = 16
LIVE_SAMPLE_COUNT = BLOCK_SAMPLES * LIVE_REFILL_COUNT
LIVE_DURATION_NS = LIVE_SAMPLE_COUNT * 1_000_000_000 // LIVE_SAMPLE_RATE_HZ
SOURCE_RECORDING_ID = RecordingId("rec_01M019X0KZK9JEPWPYATZ7SGTX")
SOURCE_RECORDING_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "a6420a620eeadac992bc7530cc1a6e570b69352b9caea11ccba861400954516c",
)
SOURCE_FEATURE_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "110ed7017b450c65eb419ded8ba03c05033ea1b13a4a40693cf5f56014025f09",
)
SOURCE_REPORT_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "fc566e0b2ac7628460b3467e487643ff61c224313f317fea987c94e1579b7e40",
)


class V5DwellRequestE2EError(RuntimeError):
    """The supervised request/capture/analysis rehearsal failed closed."""


class _FeatureCatalog:
    def __init__(self) -> None:
        self._entries: dict[FeatureSetId, CatalogedFeatureSet] = {}

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        del recording_ref, idempotency_key
        entry = CatalogedFeatureSet(projection, bundle_ref)
        existing = self._entries.get(entry.ref.feature_set_id)
        if existing is not None and existing != entry:
            raise V5DwellRequestE2EError("feature-set identity conflict")
        self._entries[entry.ref.feature_set_id] = entry
        return entry.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        entry = self._entries.get(ref.feature_set_id)
        return entry if entry is not None and entry.ref == ref else None


class _FeatureCommitter:
    def __init__(
        self,
        jobs: InMemoryJobLeaseRepository,
        features: DurableFeatureSetRepository,
    ) -> None:
        self._jobs = jobs
        self._features = features
        self.ref: FeatureSetRef | None = None
        self.bundle: FeatureSetBundle | None = None

    def commit(self, lease: Any, prepared: PreparedRecordingAnalysis) -> ArtifactRef:
        self.bundle = prepared.bundle
        self.ref = self._features.publish(
            prepared.request,
            prepared.bundle,
            idempotency_key=f"recording-analysis:{lease.job_id}",
        )
        result = ArtifactRef(
            str(self.ref.feature_set_id),
            self.ref.bundle_ref.digest,
            prepared.bundle.schema,
        )
        self._jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


class _NoOpenRadioProvider:
    def open(self) -> RadioDevice:
        raise V5DwellRequestE2EError("durable replay attempted to reopen the radio")


def live_request(now_utc_ns: int) -> DwellRequest:
    """Build the exact bounded request used by this supervised qualification."""

    observed = UtcNs(now_utc_ns - 2)
    issued = UtcNs(now_utc_ns)
    evidence = LabelEvidenceRef(
        SchemaRef(LabelEvidenceRef.SCHEMA_ID),
        "evidence_v5_scan_e2e_20260814",
        EvidenceKind.OPERATOR_NOTE,
        ArtifactRef(
            "artifact_v5_scan_e2e_report_20260814",
            SOURCE_REPORT_DIGEST,
            SchemaRef("org.leo-flow.v5-scan-e2e-report"),
        ),
        "producer_v5_scan_e2e",
        observed,
    )
    source = ScanResultRef(
        SchemaRef(ScanResultRef.SCHEMA_ID),
        "scanresult_v5_pipeline_qualification",
        SOURCE_RECORDING_ID,
        SOURCE_RECORDING_DIGEST,
        FeatureSetRef(
            FeatureSetId("fset_ce07a923f02bda6f27c33159e8bd7186"),
            AnalysisRunId("arun_208613bef4398d36b41eba07abe1c250"),
            ObjectRef(
                SOURCE_FEATURE_DIGEST,
                56_719,
                "application/json",
                "feature-set-bundle-v0.1",
                "historical-local-cas:sha256:110ed7017b450c65e",
            ),
        ),
        CAPTURE_IDENTITY.station_id,
        RADIO_ID,
        observed,
        LIVE_CENTER_FREQUENCY_HZ,
        LIVE_SAMPLE_RATE_HZ,
        LIVE_BANDWIDTH_HZ,
        (evidence,),
    )
    return DwellRequest(
        SchemaRef(DwellRequest.SCHEMA_ID),
        "dwell_v5_pipeline_qualification",
        source,
        source.station_id,
        source.radio_id,
        issued,
        UtcNs(now_utc_ns + 120_000_000_000),
        source.center_frequency_hz,
        source.sample_rate_hz,
        source.bandwidth_hz,
        LIVE_DURATION_NS,
        LIVE_SAMPLE_COUNT,
        "operator_supervised_pipeline_qualification",
        source.evidence_refs,
        "dwell:v5-pipeline-qualification:20260814",
    )


def _policy() -> DwellSafetyPolicy:
    return DwellSafetyPolicy(
        CAPTURE_IDENTITY.station_id,
        RADIO_ID,
        RECEIVER_CHAINS,
        GainSetting(GainMode.AGC),
        1_700_000_000,
        2_000_000_000,
        2_000_000,
        2_000_000,
        3_000_000_000,
        LIVE_SAMPLE_COUNT,
    )


def run_live(output_root: Path, *, confirmed_serial: str) -> dict[str, object]:
    require_live_confirmation(confirmed_serial)
    root = require_empty_output_root(output_root)
    now = time.time_ns()
    request = live_request(now)
    database = root / "capture-state.sqlite3"
    recordings_root = root / "recordings"
    spool = SQLiteLocalSpool(database, recordings_root)
    plans = SQLiteCapturePlanRepository(database)
    local = RootedSigMFRecordingStore(recordings_root)
    blobs = FileSystemBlobStore(root / "cas")
    recordings = InMemoryRecordingCatalog()
    reconciler = PublicationReconciler(
        spool, RecordingPublisherAdapter(local, blobs, recordings), local
    )
    gate = DurableDwellRequestGate(DwellRequestGate(_policy()), plans, plans)
    system_clock = SystemCaptureClock()
    clock_valid_from = UtcNs(now - 60_000_000_000)
    clock_valid_until = UtcNs(now + 300_000_000_000)
    trusted_clock = TrustedCaptureClock(
        system_clock,
        lambda: ClockAttestation(
            "operator-confirmed-host-ntp",
            True,
            clock_valid_from,
            clock_valid_until,
            1_000_000_000,
        ),
        maximum_uncertainty_ns=1_000_000_000,
    )
    scheduler = OneShotDwellCaptureScheduler(
        gate,
        V5RadioProvider(),
        PlanCaptureEngine(CAPTURE_IDENTITY, clock=trusted_clock),
        SigMFRecordingWriter(),
        spool,
        reconciler,
    )
    supervisor_state = SQLiteSupervisorState(database)
    supervisor = V5DwellSupervisor(
        host_guard=CaptureHostGuard(
            root / "run" / "capture.lock",
            (root, recordings_root, root / "cas"),
            128 * 1024 * 1024,
        ),
        clock=trusted_clock,
        spool=spool,
        local_recordings=local,
        reconciler=reconciler,
        scheduler=scheduler,
        capacity=CapacityConfiguration(
            CapacityThresholds(
                256 * 1024 * 1024,
                128 * 1024 * 1024,
                0.02,
                0.01,
            ),
            (CapacityRoot("qualification", root),),
            "critical",
        ),
        retention=RetentionCapacityPolicy(2, 128 * 1024 * 1024, 16 * 1024 * 1024),
        state=supervisor_state,
    )
    tx_before = verify_tx2_muted(EXPECTED_URI, EXPECTED_SERIAL)
    try:
        supervisor.start()
        processed = supervisor.process(request)
    finally:
        try:
            supervisor.close(10.0)
        finally:
            tx_after = verify_tx2_muted(EXPECTED_URI, EXPECTED_SERIAL)
    receipt = processed.schedule
    durable_receipt = processed.durable_receipt
    supervisor_health = supervisor_state.health()
    if supervisor_health is None or supervisor_health.state != "stopped":
        raise V5DwellRequestE2EError("capture supervisor did not stop durably")
    if not receipt.captured_now or receipt.published_now != 1:
        raise V5DwellRequestE2EError("first request did not capture and publish once")
    published = recordings.get(str(receipt.recording_id))
    if published is None:
        raise V5DwellRequestE2EError("published dwell is absent from local catalog")

    with SigMFRecordingObjectReader(blobs).open(
        published.recording_object
    ) as recording_view:
        if (
            canonical_digest(recording_view.manifest)
            != published.recording_object.manifest_digest
        ):
            raise V5DwellRequestE2EError("recording manifest identity changed")
        segment = recording_view.manifest.segments[0]
        continuity = recording_view.continuity(segment.segment_id)
        if continuity is None:
            raise V5DwellRequestE2EError("dwell continuity metadata is absent")
        frame_accounting = sustained_continuity_evidence(
            segment, continuity, expected_refill_count=LIVE_REFILL_COUNT
        )
    integrity = object_integrity_evidence(blobs, published.recording_object, segment)

    restarted_plans = SQLiteCapturePlanRepository(database)
    restarted_spool = SQLiteLocalSpool(database, recordings_root)
    replay_gate = DurableDwellRequestGate(
        DwellRequestGate(_policy()), restarted_plans, restarted_plans
    )
    replay = OneShotDwellCaptureScheduler(
        replay_gate,
        _NoOpenRadioProvider(),
        PlanCaptureEngine(CAPTURE_IDENTITY),
        SigMFRecordingWriter(),
        restarted_spool,
        PublicationReconciler(
            restarted_spool,
            RecordingPublisherAdapter(local, blobs, recordings),
            local,
        ),
    ).run(request, UtcNs(now + 2))
    if replay.captured_now or replay.recording_id != receipt.recording_id:
        raise V5DwellRequestE2EError("durable request replay attempted recapture")

    config = QualityPsdConfig(
        psd_window_samples=256,
        psd_stride_samples=262_144,
        clip_threshold_abs=32_760,
    )
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: now + 3)
    submission_service = RecordingAnalysisSubmissionService(jobs)
    submission = RecordingAnalysisSubmission(
        published,
        quality_psd_algorithm_ref(),
        quality_psd_config_ref(config),
        (),
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    submitted = submission_service.submit(submission)
    assert submission_service.submit(submission).job_id == submitted.job_id
    feature_catalog = _FeatureCatalog()
    features = DurableFeatureSetRepository(blobs, feature_catalog)
    committer = _FeatureCommitter(jobs, features)
    execution = AnalysisExecutionContext(
        "v5-dwell-request-e2e-quality-psd",
        "0.1.0",
        "working-tree",
        Digest.sha256(b"leo-flow-v5-qualified-runtime"),
        UtcNs(now + 3),
        UtcNs(now + 4),
        "qualified-v5-container",
    )
    analyzer = QualityPsdAnalyzer(config, execution)
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(SigMFRecordingObjectReader(blobs), analyzer),
        committer,
        worker_id="v5-dwell-request-e2e",
        lease_ttl_s=300,
    )
    if not worker.process_one_job() or worker.process_one_job():
        raise V5DwellRequestE2EError("analysis job execution was not exactly once")
    if jobs.snapshot(submitted.job_id).state is not JobState.SUCCEEDED:
        raise V5DwellRequestE2EError("analysis job did not reach succeeded")
    if committer.ref is None or committer.bundle is None:
        raise V5DwellRequestE2EError("analysis did not publish a FeatureSet")
    replay_ref = features.publish(
        submitted.request,
        committer.bundle,
        idempotency_key=f"recording-analysis:{submitted.job_id}",
    )
    if replay_ref != committer.ref:
        raise V5DwellRequestE2EError("FeatureSet idempotent replay changed identity")
    restarted_features = DurableFeatureSetRepository(
        FileSystemBlobStore(root / "cas"), feature_catalog
    )
    with restarted_features.open(committer.ref) as feature_view:
        bundle = feature_view.bundle()

    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "pass",
        "mode": "analysis-request-to-passive-dwell-to-independent-analysis",
        "radio_uri": EXPECTED_URI,
        "dwell_request": {
            "request_id": request.request_id,
            "request_digest": str(canonical_digest(request)),
            "source_scan_result_id": request.source.result_id,
            "idempotency_key": request.idempotency_key,
            "expires_utc_ns": request.expires_utc_ns,
        },
        "capture": {
            "plan_id": str(receipt.plan_id),
            "plan_digest": str(receipt.plan_digest),
            "recording_id": str(receipt.recording_id),
            "recording_identity_digest": str(
                published.recording_object.identity_digest()
            ),
            "captured_now": receipt.captured_now,
            "restart_replay_captured": replay.captured_now,
            "restart_replay_recording_id": str(replay.recording_id),
            "frame_accounting": frame_accounting,
            "object_integrity": integrity,
            "durable_receipt_digest": str(durable_receipt.identity_digest()),
            "supervisor_health": {
                "state": supervisor_health.state,
                "ready": supervisor_health.ready,
                "completed_units": supervisor_health.completed_units,
                "failed_units": supervisor_health.failed_units,
                "startup_published": supervisor_health.startup_published,
                "startup_cleaned": supervisor_health.startup_cleaned,
                "capacity_status": supervisor_health.capacity_status,
                "clock_source": supervisor_health.clock_source,
            },
        },
        "analysis": {
            "job_id": str(submitted.job_id),
            "job_state": jobs.snapshot(submitted.job_id).state.value,
            "duplicate_submission_same_job": True,
            "second_worker_claimed_job": False,
            "feature_set_id": str(bundle.feature_set_id),
            "analysis_run_id": str(bundle.analysis_run_id),
            "feature_bundle_digest": str(committer.ref.bundle_ref.digest),
            "feature_bundle_byte_count": committer.ref.bundle_ref.byte_count,
            "idempotent_replay_same_ref": replay_ref == committer.ref,
            "restarted_blob_reader_verified": True,
            "observation_count": len(bundle.observations),
            "method_score_count": len(bundle.method_scores),
            "warnings": list(bundle.warnings),
            "reason_codes": list(bundle.reason_codes),
        },
        "tx_evidence": {"before_capture": tx_before, "after_capture": tx_after},
        "storage": {
            "capture_plan_journal": "sqlite",
            "capture_spool": "sqlite",
            "recording_and_feature_blobs": "local_filesystem_cas",
            "catalogs": "in_memory_e2e_only",
            "external_write_attempted": False,
        },
        "truth": {
            "kind": "passive-no-lnb-baseline",
            "known_signal_present": None,
            "scientific_detection_claim": False,
        },
    }
    (root / "dwell-request-e2e-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--confirm-radio-serial", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        raise V5DwellRequestE2EError("--live is required; use pytest for fakes")
    report = run_live(
        arguments.output_root, confirmed_serial=arguments.confirm_radio_serial
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
