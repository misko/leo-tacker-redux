"""Explicitly armed PostgreSQL-to-V5-to-PostgreSQL qualification."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from leo_flow.adapters.dwell_postgres import (
    ConnectionFactory,
    DwellRequestLease,
    PostgresDwellRequestIngress,
    PostgresDwellRequestQueue,
)
from leo_flow.adapters.feature_postgres_catalog import PostgresFeatureSetCatalog
from leo_flow.adapters.recording_analysis_postgres import (
    AtomicPostgresRecordingAnalysisCommitter,
)
from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.analysis.recording.codec import encode_feature_set
from leo_flow.application import (
    DurableDwellRequestGate,
    DwellRequestGate,
    DwellSafetyPolicy,
)
from leo_flow.capture.clock import SystemCaptureClock
from leo_flow.capture.engine import PlanCaptureEngine
from leo_flow.capture.plan_repository import SQLiteCapturePlanRepository
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.capture import CapturePlanRef, GainMode, GainSetting
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dwell import DwellRequest, ScanResultRef
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.contracts.features import (
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.ports import RadioDevice
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments.v5_canary import CaptureHostGuard, V5RadioProvider
from leo_flow.deployments.v5_dwell_e2e import (
    BLOCK_SAMPLES,
    CAPTURE_IDENTITY,
    EXPECTED_SERIAL,
    EXPECTED_URI,
    RADIO_ID,
    RECEIVER_CHAINS,
    object_integrity_evidence,
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
from leo_flow.jobs import JobState
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
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
    SubmittedRecordingAnalysis,
)
from leo_flow.storage.filesystem import FileSystemBlobReader, FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.postgres_catalog import (
    PostgresRecordingCatalog,
    PostgresRecordingPublisher,
)
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)

REPORT_SCHEMA = "org.leo-flow.v5-production-path-qualification/v2"
EXPECTED_CLOCK_CONFIRMATION = "host-ntp-synchronized"
EXPECTED_MIGRATION_HEAD = "0019_dwell_request_ingress.sql"
EXPECTED_POSTGRES_MAJOR = 16
MAX_DSN_BYTES = 4096
_DATABASE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{10,20}")
_LOCAL_FILESYSTEM_TYPES = frozenset(
    {
        "bcachefs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "overlay",
        "tmpfs",
        "xfs",
        "zfs",
    }
)
_CAPABILITY_ROLES = (
    "leo_capture",
    "leo_analysis",
    "leo_dashboard",
    "leo_maintenance",
    "leo_routine_owner",
)
_APPLICATION_TABLES = (
    "dashboard_activity_projection",
    "dashboard_analysis_projection_identity",
    "dashboard_capture_projection_identity",
    "dashboard_feature_projection",
    "dashboard_model_projection",
    "dashboard_recording_projection",
    "dashboard_storage_health_projection",
    "dashboard_track_projection",
    "dataset_member",
    "dataset_snapshot",
    "detector_evaluation_method_summary",
    "detector_evaluation_report",
    "dwell_request_ingress",
    "ephemeris_snapshot",
    "feature_set",
    "hardware_radio",
    "hardware_receiver_chain",
    "hardware_snapshot",
    "job",
    "model_release",
    "model_snapshot",
    "object_blob",
    "object_gc_attempt",
    "object_orphan_event",
    "object_orphan_observation",
    "object_retention_assignment",
    "object_retention_policy",
    "recording",
    "recording_ephemeris_link",
    "recording_hardware_link",
    "tracking_input_entry",
    "tracking_input_snapshot",
    "tracking_model_snapshot",
)
_FINAL_TABLE_COUNTS = {
    **{name: 0 for name in _APPLICATION_TABLES},
    "dwell_request_ingress": 1,
    "feature_set": 2,
    "job": 3,
    "object_blob": 6,
    "recording": 2,
}
DWELL_REQUEST_ID = "dwell_v5_postgres_qualification"
LIVE_SAMPLE_RATE_HZ = 2_000_000
LIVE_BANDWIDTH_HZ = 1_800_000
LIVE_CENTER_FREQUENCY_HZ = 1_825_000_000
LIVE_REFILL_COUNT = 16
LIVE_SAMPLE_COUNT = BLOCK_SAMPLES * LIVE_REFILL_COUNT
LIVE_DURATION_NS = LIVE_SAMPLE_COUNT * 1_000_000_000 // LIVE_SAMPLE_RATE_HZ
SOURCE_OBSERVED_UTC_NS = UtcNs(1_786_750_345_798_029_029)
SOURCE_RECORDING_ID = RecordingId("rec_01M019X0KZK9JEPWPYATZ7SGTX")
SOURCE_DATA = ObjectRef(
    Digest(
        DigestAlgorithm.SHA256,
        "7fd0b9d0a16b4dc00fab68c83b93660eec1eec824d1a8d977876270b99f92365",
    ),
    16_777_216,
    "application/octet-stream",
    "leo-recording-data-v1",
    "cas:sha256:7fd0b9d0a16b4dc00fab68c83b93660eec1eec824d1a8d977876270b99f92365",
)
SOURCE_METADATA = ObjectRef(
    Digest(
        DigestAlgorithm.SHA256,
        "9d81565198acafcbe67f60871b93c7b135f548219b81e9630c18c878f0e6e15f",
    ),
    28_660,
    "application/json",
    "leo-recording-metadata-v1",
    "cas:sha256:9d81565198acafcbe67f60871b93c7b135f548219b81e9630c18c878f0e6e15f",
)
SOURCE_RECORDING = RecordingObjectRef(
    SOURCE_RECORDING_ID,
    SOURCE_DATA,
    SOURCE_METADATA,
    Digest(
        DigestAlgorithm.SHA256,
        "e10e82d1bbe3e72b245d5bfd3c0621eb9b3590913a115603bc1128c65ced3d46",
    ),
)
SOURCE_IDENTITY = Digest(
    DigestAlgorithm.SHA256,
    "a6420a620eeadac992bc7530cc1a6e570b69352b9caea11ccba861400954516c",
)
SOURCE_REPORT_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "fc566e0b2ac7628460b3467e487643ff61c224313f317fea987c94e1579b7e40",
)
if SOURCE_RECORDING.identity_digest() != SOURCE_IDENTITY:
    raise RuntimeError("qualified source-scan identity changed")


class ProductionPathQualificationError(RuntimeError):
    """The explicitly armed composed qualification failed closed."""


class _RadioProvider(Protocol):
    def open(self) -> RadioDevice: ...


@dataclass(frozen=True)
class QualificationProfile:
    sample_rate_hz: int
    bandwidth_hz: int
    center_frequency_hz: int
    refill_count: int
    sample_count: int
    duration_ns: int


LIVE_PROFILE = QualificationProfile(
    LIVE_SAMPLE_RATE_HZ,
    LIVE_BANDWIDTH_HZ,
    LIVE_CENTER_FREQUENCY_HZ,
    LIVE_REFILL_COUNT,
    LIVE_SAMPLE_COUNT,
    LIVE_DURATION_NS,
)


@dataclass(frozen=True)
class PostgresIdentity:
    database_name: str
    database_owner: str
    system_identifier: str
    server_major: int = EXPECTED_POSTGRES_MAJOR

    def __post_init__(self) -> None:
        if (
            _DATABASE_TOKEN.fullmatch(self.database_name) is None
            or _DATABASE_TOKEN.fullmatch(self.database_owner) is None
            or _SYSTEM_IDENTIFIER.fullmatch(self.system_identifier) is None
            or self.server_major != EXPECTED_POSTGRES_MAJOR
        ):
            raise ProductionPathQualificationError(
                "PostgreSQL identity must pin a PostgreSQL 16 database, owner, "
                "and cluster system identifier"
            )


@dataclass(frozen=True)
class QualificationInputs:
    output_root: Path
    source_cas_root: Path
    capture_connect: ConnectionFactory
    analysis_connect: ConnectionFactory
    audit_connect: ConnectionFactory
    radio_provider: _RadioProvider
    tx_verifier: Callable[[], dict[str, object]]
    clock_source: str
    postgres_identity: PostgresIdentity
    migration_directory: Path
    stale_lease_wait_s: float = 0.08
    profile: QualificationProfile = LIVE_PROFILE
    source_recording: RecordingObjectRef = SOURCE_RECORDING


class _NoOpenRadioProvider:
    def open(self) -> RadioDevice:
        raise ProductionPathQualificationError(
            "durable replay attempted to reopen the radio"
        )


class _ObservedAnalysisCommitter:
    """Observe the public commit output without replacing its atomic committer."""

    def __init__(
        self,
        delegate: AtomicPostgresRecordingAnalysisCommitter,
        blobs: FileSystemBlobStore,
    ) -> None:
        self._delegate = delegate
        self._blobs = blobs
        self.feature_ref: FeatureSetRef | None = None
        self.bundle: FeatureSetBundle | None = None

    def commit(self, lease: object, prepared: PreparedRecordingAnalysis) -> ArtifactRef:
        from leo_flow.jobs.contracts import JobLease

        if not isinstance(lease, JobLease):
            raise TypeError("recording-analysis lease type changed")
        result = self._delegate.commit(lease, prepared)
        payload = encode_feature_set(prepared.bundle)
        bundle_ref = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=result.digest,
            expected_bytes=len(payload),
            media_type="application/json",
            format_id="feature-set-bundle-v0.1",
            idempotency_key=f"qualification-observe:{lease.job_id}",
        )
        feature_ref = FeatureSetRef(
            prepared.bundle.feature_set_id,
            prepared.bundle.analysis_run_id,
            bundle_ref,
        )
        if result.artifact_id != str(feature_ref.feature_set_id):
            raise ProductionPathQualificationError(
                "analysis result and FeatureSet identity differ"
            )
        self.feature_ref = feature_ref
        self.bundle = prepared.bundle
        return result


def run_qualification(inputs: QualificationInputs) -> dict[str, object]:
    """Execute one exact source-analysis → dwell → dwell-analysis path."""

    root = _require_empty_local_root(inputs.output_root)
    source_filesystem = _require_non_nfs(inputs.source_cas_root)
    output_filesystem = _require_non_nfs(root)
    role_evidence = {
        "capture": _role_evidence(inputs.capture_connect, "leo_capture"),
        "analysis": _role_evidence(inputs.analysis_connect, "leo_analysis"),
    }
    database_preflight = _database_preflight(inputs)
    migrations = database_preflight["migrations"]
    blobs = FileSystemBlobStore(root / "cas")

    source = _import_source_recording(
        FileSystemBlobReader(inputs.source_cas_root),
        blobs,
        PostgresRecordingCatalog(inputs.capture_connect),
        inputs.source_recording,
    )
    source_analysis = _run_analysis(
        source,
        blobs,
        inputs.analysis_connect,
        producer="v5-production-path-source-analysis",
    )

    now = time.time_ns()
    request = _dwell_request(source, source_analysis.feature_ref, now, inputs.profile)
    ingress = PostgresDwellRequestIngress(inputs.analysis_connect)
    published_request = ingress.publish(request)
    queue = PostgresDwellRequestQueue(
        inputs.capture_connect, token_factory=lambda: "lease_wave7_stale"
    )
    stale = queue.claim(request.station_id, request.radio_id, 0.05)
    if stale is None:
        raise ProductionPathQualificationError("published dwell was not claimable")
    time.sleep(inputs.stale_lease_wait_s)
    active_queue = PostgresDwellRequestQueue(
        inputs.capture_connect, token_factory=lambda: "lease_wave7_active"
    )
    active = active_queue.claim(request.station_id, request.radio_id, 120.0)
    if active is None or active.lease_generation != stale.lease_generation + 1:
        raise ProductionPathQualificationError("expired dwell lease was not fenced")
    stale_rejected = _stale_heartbeat_rejected(queue, stale)
    active = active_queue.heartbeat(active, 120.0)

    tx_before = inputs.tx_verifier()
    supervisor, state, spool = _supervisor(
        root,
        blobs,
        inputs.capture_connect,
        inputs.radio_provider,
        inputs.clock_source,
        inputs.profile,
    )
    try:
        supervisor.start()
        processed = supervisor.process(active.request)
        active_queue.complete(
            active,
            CapturePlanRef(processed.schedule.plan_id, processed.schedule.plan_digest),
        )
    finally:
        try:
            supervisor.close(10.0)
        finally:
            tx_after = inputs.tx_verifier()
    health = state.health()
    if health is None or health.state != "stopped" or health.failed_units:
        raise ProductionPathQualificationError("capture health did not close cleanly")
    if processed.schedule.spool_state is not SpoolState.CLEANED:
        raise ProductionPathQualificationError("capture spool did not clean locally")

    catalog = PostgresRecordingCatalog(inputs.analysis_connect)
    dwell_recording = catalog.get(processed.schedule.recording_id)
    if dwell_recording is None:
        raise ProductionPathQualificationError("dwell recording is not cataloged")
    continuity, integrity = _verify_dwell(blobs, dwell_recording, inputs.profile)
    local_cleanup = _local_cleanup_evidence(root / "recordings", spool)

    dwell_analysis = _run_analysis(
        dwell_recording,
        blobs,
        inputs.analysis_connect,
        producer="v5-production-path-dwell-analysis",
    )
    if (
        dwell_analysis.bundle.recording_id != dwell_recording.recording_id
        or dwell_analysis.bundle.input_recording_identity_digest
        != dwell_recording.recording_object.identity_digest()
    ):
        raise ProductionPathQualificationError("FeatureSet lineage is not exact")

    replay = _prove_fresh_replay(
        root,
        blobs,
        inputs,
        request,
        published_request,
        processed.schedule.recording_id,
        dwell_analysis,
    )
    database = _database_evidence(
        inputs.audit_connect,
        published_request.job_id,
        source_analysis.submission.job_id,
        dwell_analysis.submission.job_id,
        source.recording_id,
        dwell_recording.recording_id,
        source_analysis.feature_ref,
        dwell_analysis.feature_ref,
        CapturePlanRef(processed.schedule.plan_id, processed.schedule.plan_digest),
    )

    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "pass",
        "mode": "postgres-ingress-to-passive-dwell-to-postgres-analysis",
        "radio_uri": EXPECTED_URI,
        "migrations": migrations,
        "roles": role_evidence,
        "postgres_identity": database_preflight["identity"],
        "database_preflight": {
            "application_catalog_empty": True,
            "initial_table_counts": database_preflight["initial_table_counts"],
            "read_only": True,
        },
        "filesystem": {
            "source": source_filesystem,
            "output": output_filesystem,
            "approved_local_filesystems_only": True,
        },
        "source": {
            "recording_id": str(source.recording_id),
            "recording_identity_digest": str(source.recording_object.identity_digest()),
            "analysis_job_id": str(source_analysis.submission.job_id),
            "feature_set_ref": _feature_ref_evidence(source_analysis.feature_ref),
        },
        "dwell_request": {
            "job_id": str(published_request.job_id),
            "request_id": request.request_id,
            "request_digest": str(published_request.request_digest),
            "idempotency_key": request.idempotency_key,
            "stale_lease": _lease_evidence(stale),
            "active_lease": _lease_evidence(active),
            "stale_heartbeat_rejected": stale_rejected,
            "heartbeat_succeeded": True,
            "completed": True,
        },
        "capture": {
            "plan_id": str(processed.schedule.plan_id),
            "plan_digest": str(processed.schedule.plan_digest),
            "recording_id": str(processed.schedule.recording_id),
            "recording_identity_digest": str(
                dwell_recording.recording_object.identity_digest()
            ),
            "durable_receipt_digest": str(processed.durable_receipt.identity_digest()),
            "continuity": continuity,
            "integrity": integrity,
            "local_cleanup": local_cleanup,
            "health": {
                "state": health.state,
                "completed_units": health.completed_units,
                "failed_units": health.failed_units,
                "capacity_status": health.capacity_status,
                "clock_source": health.clock_source,
            },
        },
        "analysis": {
            "job_id": str(dwell_analysis.submission.job_id),
            "job_state": dwell_analysis.job_state,
            "feature_set_ref": _feature_ref_evidence(dwell_analysis.feature_ref),
            "recording_id": str(dwell_analysis.bundle.recording_id),
            "input_recording_identity_digest": str(
                dwell_analysis.bundle.input_recording_identity_digest
            ),
            "observation_count": len(dwell_analysis.bundle.observations),
            "method_score_count": len(dwell_analysis.bundle.method_scores),
        },
        "fresh_process_replay": replay,
        "database": database,
        "tx_evidence": {
            "before_capture": tx_before,
            "after_capture": tx_after,
        },
        "truth": {
            "kind": "passive-no-lnb-baseline",
            "known_signal_present": None,
            "scientific_detection_claim": False,
        },
    }
    report_path = root / "production-path-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


@dataclass(frozen=True)
class _AnalysisResult:
    submission: SubmittedRecordingAnalysis
    feature_ref: FeatureSetRef
    bundle: FeatureSetBundle
    job_state: str


def _run_analysis(
    recording: PublishedRecordingRef,
    blobs: FileSystemBlobStore,
    connect: ConnectionFactory,
    *,
    producer: str,
) -> _AnalysisResult:
    config = QualityPsdConfig(
        psd_window_samples=256,
        psd_stride_samples=262_144,
        clip_threshold_abs=32_760,
    )
    jobs = PostgresJobLeaseRepository(connect)
    submission = RecordingAnalysisSubmissionService(jobs).submit(
        RecordingAnalysisSubmission(
            recording,
            quality_psd_algorithm_ref(),
            quality_psd_config_ref(config),
            (),
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
    )
    now = time.time_ns()
    observed = _ObservedAnalysisCommitter(
        AtomicPostgresRecordingAnalysisCommitter(blobs, connect), blobs
    )
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            SigMFRecordingObjectReader(blobs),
            QualityPsdAnalyzer(
                config,
                AnalysisExecutionContext(
                    producer,
                    "0.1.0",
                    "working-tree",
                    Digest.sha256(b"leo-flow-v5-qualified-runtime"),
                    UtcNs(now),
                    UtcNs(now + 1),
                    "qualified-v5-container",
                ),
            ),
        ),
        observed,
        worker_id=producer,
        lease_ttl_s=120,
    )
    if not worker.process_one_job():
        raise ProductionPathQualificationError("analysis job was not claimed")
    snapshot = jobs.snapshot(submission.job_id)
    if (
        snapshot.state is not JobState.SUCCEEDED
        or observed.feature_ref is None
        or observed.bundle is None
        or snapshot.result_ref is None
        or snapshot.result_ref.digest != observed.feature_ref.bundle_ref.digest
    ):
        raise ProductionPathQualificationError(
            "analysis did not atomically publish and complete"
        )
    cataloged = PostgresFeatureSetCatalog(connect).get(observed.feature_ref)
    if cataloged is None or cataloged.ref != observed.feature_ref:
        raise ProductionPathQualificationError("FeatureSet catalog readback failed")
    return _AnalysisResult(
        submission,
        observed.feature_ref,
        observed.bundle,
        snapshot.state.value,
    )


def _dwell_request(
    source: PublishedRecordingRef,
    feature: FeatureSetRef,
    now_utc_ns: int,
    profile: QualificationProfile = LIVE_PROFILE,
) -> DwellRequest:
    evidence = (
        LabelEvidenceRef(
            SchemaRef(LabelEvidenceRef.SCHEMA_ID),
            "evidence_v5_postgres_qualification",
            EvidenceKind.OPERATOR_NOTE,
            ArtifactRef(
                "artifact_v5_scan_e2e_report_20260814",
                SOURCE_REPORT_DIGEST,
                SchemaRef("org.leo-flow.v5-scan-e2e-report"),
            ),
            "producer_v5_scan_e2e",
            SOURCE_OBSERVED_UTC_NS,
        ),
    )
    scan = ScanResultRef(
        SchemaRef(ScanResultRef.SCHEMA_ID),
        "scanresult_v5_postgres_qualification",
        source.recording_id,
        source.recording_object.identity_digest(),
        feature,
        CAPTURE_IDENTITY.station_id,
        RADIO_ID,
        SOURCE_OBSERVED_UTC_NS,
        profile.center_frequency_hz,
        profile.sample_rate_hz,
        profile.bandwidth_hz,
        evidence,
    )
    return DwellRequest(
        SchemaRef(DwellRequest.SCHEMA_ID),
        DWELL_REQUEST_ID,
        scan,
        scan.station_id,
        scan.radio_id,
        UtcNs(now_utc_ns),
        UtcNs(now_utc_ns + 240_000_000_000),
        scan.center_frequency_hz,
        scan.sample_rate_hz,
        scan.bandwidth_hz,
        profile.duration_ns,
        profile.sample_count,
        "postgres_pipeline_qualification",
        evidence,
        "dwell:v5-postgres-qualification:20260814",
    )


def _policy(profile: QualificationProfile) -> DwellSafetyPolicy:
    return DwellSafetyPolicy(
        CAPTURE_IDENTITY.station_id,
        RADIO_ID,
        RECEIVER_CHAINS,
        GainSetting(GainMode.AGC),
        1_700_000_000,
        2_000_000_000,
        max(2_000_000, profile.sample_rate_hz),
        max(2_000_000, profile.bandwidth_hz),
        3_000_000_000,
        max(LIVE_SAMPLE_COUNT, profile.sample_count),
    )


def _supervisor(
    root: Path,
    blobs: FileSystemBlobStore,
    connect: ConnectionFactory,
    provider: _RadioProvider,
    clock_source: str,
    profile: QualificationProfile,
) -> tuple[V5DwellSupervisor, SQLiteSupervisorState, SQLiteLocalSpool]:
    database = root / "capture-state.sqlite3"
    recordings_root = root / "recordings"
    spool = SQLiteLocalSpool(database, recordings_root)
    plans = SQLiteCapturePlanRepository(database)
    local = RootedSigMFRecordingStore(recordings_root)
    catalog = PostgresRecordingCatalog(connect)
    reconciler = PublicationReconciler(
        spool,
        PostgresRecordingPublisher(local, blobs, catalog),
        local,
    )
    system_clock = SystemCaptureClock()
    now = system_clock.now_utc_ns()
    trusted_clock = TrustedCaptureClock(
        system_clock,
        lambda: ClockAttestation(
            clock_source,
            True,
            UtcNs(now - 60_000_000_000),
            UtcNs(now + 300_000_000_000),
            1_000_000_000,
        ),
        maximum_uncertainty_ns=1_000_000_000,
    )
    scheduler = OneShotDwellCaptureScheduler(
        DurableDwellRequestGate(DwellRequestGate(_policy(profile)), plans, plans),
        provider,
        PlanCaptureEngine(CAPTURE_IDENTITY, clock=trusted_clock),
        SigMFRecordingWriter(),
        spool,
        reconciler,
    )
    state = SQLiteSupervisorState(database)
    return (
        V5DwellSupervisor(
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
            state=state,
        ),
        state,
        spool,
    )


def _import_source_recording(
    source: FileSystemBlobReader,
    target: FileSystemBlobStore,
    catalog: PostgresRecordingCatalog,
    expected: RecordingObjectRef,
) -> PublishedRecordingRef:
    copied: list[ObjectRef] = []
    for name, ref in (
        ("data", expected.data_object),
        ("metadata", expected.metadata_object),
    ):
        source.head(ref)
        with source.open(ref) as stream:
            copied.append(
                target.put(
                    stream,
                    expected_digest=ref.digest,
                    expected_bytes=ref.byte_count,
                    media_type=ref.media_type,
                    format_id=ref.format_id,
                    idempotency_key=f"qualified-source-scan:{name}",
                )
            )
    recording = RecordingObjectRef(
        expected.recording_id,
        copied[0],
        copied[1],
        expected.manifest_digest,
    )
    if recording.identity_digest() != expected.identity_digest():
        raise ProductionPathQualificationError("copied source identity changed")
    return catalog.publish(recording, idempotency_key="qualified-source-scan")


def _verify_dwell(
    blobs: FileSystemBlobStore,
    published: PublishedRecordingRef,
    profile: QualificationProfile,
) -> tuple[dict[str, object], dict[str, object]]:
    with SigMFRecordingObjectReader(blobs).open(
        published.recording_object
    ) as recording:
        if len(recording.manifest.segments) != 1:
            raise ProductionPathQualificationError("dwell segment count changed")
        segment = recording.manifest.segments[0]
        continuity = recording.continuity(segment.segment_id)
        if continuity is None:
            raise ProductionPathQualificationError("dwell continuity is absent")
        continuity_evidence = sustained_continuity_evidence(
            segment, continuity, expected_refill_count=profile.refill_count
        )
    integrity = object_integrity_evidence(blobs, published.recording_object, segment)
    return continuity_evidence, integrity


def _prove_fresh_replay(
    root: Path,
    blobs: FileSystemBlobStore,
    inputs: QualificationInputs,
    request: DwellRequest,
    first_publication: object,
    recording_id: RecordingId,
    analysis: _AnalysisResult,
) -> dict[str, object]:
    second_publication = PostgresDwellRequestIngress(inputs.analysis_connect).publish(
        request
    )
    if second_publication != first_publication:
        raise ProductionPathQualificationError("ingress replay changed identity")
    if (
        PostgresDwellRequestQueue(inputs.capture_connect).claim(
            request.station_id, request.radio_id, 5
        )
        is not None
    ):
        raise ProductionPathQualificationError("completed dwell was claimed again")

    supervisor, _state, _spool = _supervisor(
        root,
        FileSystemBlobStore(root / "cas"),
        inputs.capture_connect,
        _NoOpenRadioProvider(),
        inputs.clock_source,
        inputs.profile,
    )
    try:
        supervisor.start()
        replay = supervisor.process(request)
    finally:
        supervisor.close(10.0)
    if replay.schedule.captured_now or replay.schedule.recording_id != recording_id:
        raise ProductionPathQualificationError("fresh capture replay recaptured")

    jobs = PostgresJobLeaseRepository(inputs.analysis_connect)
    repeated = RecordingAnalysisSubmissionService(jobs).submit(
        RecordingAnalysisSubmission(
            PostgresRecordingCatalog(inputs.analysis_connect).get(recording_id)
            or _missing_recording(),
            quality_psd_algorithm_ref(),
            quality_psd_config_ref(
                QualityPsdConfig(
                    psd_window_samples=256,
                    psd_stride_samples=262_144,
                    clip_threshold_abs=32_760,
                )
            ),
            (),
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
    )
    if repeated.job_id != analysis.submission.job_id:
        raise ProductionPathQualificationError("analysis replay changed job identity")
    no_work = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            SigMFRecordingObjectReader(blobs),
            QualityPsdAnalyzer(
                QualityPsdConfig(
                    psd_window_samples=256,
                    psd_stride_samples=262_144,
                    clip_threshold_abs=32_760,
                ),
                AnalysisExecutionContext(
                    "v5-production-path-replay",
                    "0.1.0",
                    "working-tree",
                    Digest.sha256(b"leo-flow-v5-qualified-runtime"),
                    UtcNs(time.time_ns()),
                    UtcNs(time.time_ns() + 1),
                    "qualified-v5-container",
                ),
            ),
        ),
        AtomicPostgresRecordingAnalysisCommitter(blobs, inputs.analysis_connect),
        worker_id="v5-production-path-replay",
        lease_ttl_s=30,
    ).process_one_job()
    if no_work:
        raise ProductionPathQualificationError("analysis replay duplicated work")
    return {
        "ingress_same_receipt": True,
        "queue_reclaimed_completed_request": False,
        "capture_reopened_radio": False,
        "recording_id": str(replay.schedule.recording_id),
        "durable_receipt_digest": str(replay.durable_receipt.identity_digest()),
        "analysis_job_id": str(repeated.job_id),
        "analysis_worker_claimed_duplicate": False,
        "feature_set_ref": _feature_ref_evidence(analysis.feature_ref),
    }


def _missing_recording() -> PublishedRecordingRef:
    raise ProductionPathQualificationError("fresh catalog lost dwell recording")


def _stale_heartbeat_rejected(
    queue: PostgresDwellRequestQueue, lease: DwellRequestLease
) -> bool:
    try:
        queue.heartbeat(lease, 5)
    except StaleLeaseError:
        return True
    raise ProductionPathQualificationError("stale dwell lease was accepted")


def _local_cleanup_evidence(
    recording_root: Path, spool: SQLiteLocalSpool
) -> dict[str, object]:
    durable = spool.durable_recordings(2)
    files = tuple(path for path in recording_root.rglob("*") if path.is_file())
    if len(durable) != 1 or durable[0].state is not SpoolState.CLEANED or files:
        raise ProductionPathQualificationError("local capture cleanup is incomplete")
    return {
        "spool_state": durable[0].state.value,
        "remaining_recording_files": 0,
        "local_recording_pair_removed": True,
    }


def _role_evidence(connect: ConnectionFactory, expected: str) -> dict[str, object]:
    if expected not in {"leo_capture", "leo_analysis"}:
        raise ProductionPathQualificationError("runtime capability is unsupported")
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            """
            SELECT current_user AS current_user_name,
                   session_user AS session_user_name,
                   r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                   r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolinherit
              FROM pg_roles AS r WHERE r.rolname = session_user
            """
        ).fetchone()
        memberships = (
            {
                role: _membership(connection, str(row["session_user_name"]), role)
                for role in _CAPABILITY_ROLES
            }
            if row is not None
            else {}
        )
        inherited_roles = tuple(
            str(item["role_name"])
            for item in connection.execute(
                """
                WITH RECURSIVE login AS (
                    SELECT oid FROM pg_roles WHERE rolname = session_user
                ), member_of(roleid) AS (
                    SELECT membership.roleid
                      FROM pg_auth_members AS membership, login
                     WHERE membership.member = login.oid
                    UNION
                    SELECT membership.roleid
                      FROM pg_auth_members AS membership
                      JOIN member_of ON membership.member = member_of.roleid
                )
                SELECT role.rolname AS role_name
                  FROM member_of JOIN pg_roles AS role ON role.oid = member_of.roleid
                 ORDER BY role.rolname
                """
            ).fetchall()
        )
        direct_grants = tuple(
            f"{item['object_kind']}:{item['object_identity']}:{item['privilege_type']}"
            for item in connection.execute(
                """
                WITH login AS (
                    SELECT oid FROM pg_roles WHERE rolname = session_user
                ), direct_grant AS (
                    SELECT 'relation'::text AS object_kind,
                           format('%I.%I', namespace.nspname, relation.relname)
                               AS object_identity,
                           acl.privilege_type
                      FROM pg_class AS relation
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                    UNION ALL
                    SELECT 'column',
                           format('%I.%I.%I', namespace.nspname, relation.relname,
                                  attribute.attname),
                           acl.privilege_type
                      FROM pg_attribute AS attribute
                      JOIN pg_class AS relation ON relation.oid = attribute.attrelid
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                    UNION ALL
                    SELECT 'function',
                           format('%I.%I()', namespace.nspname, routine.proname),
                           acl.privilege_type
                      FROM pg_proc AS routine
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = routine.pronamespace
                      CROSS JOIN LATERAL aclexplode(routine.proacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                    UNION ALL
                    SELECT 'schema', format('%I', namespace.nspname),
                           acl.privilege_type
                      FROM pg_namespace AS namespace
                      CROSS JOIN LATERAL aclexplode(namespace.nspacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                    UNION ALL
                    SELECT 'database', format('%I', database.datname),
                           acl.privilege_type
                      FROM pg_database AS database
                      CROSS JOIN LATERAL aclexplode(database.datacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                    UNION ALL
                    SELECT 'default_acl', default_acl.oid::text,
                           acl.privilege_type
                      FROM pg_default_acl AS default_acl
                      CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) AS acl
                      JOIN login ON login.oid = acl.grantee
                )
                SELECT object_kind, object_identity, privilege_type
                  FROM direct_grant
                 ORDER BY object_kind, object_identity, privilege_type
                """
            ).fetchall()
        )
        shared_dependencies = connection.execute(
            """
            WITH login AS (SELECT oid FROM pg_roles WHERE rolname = session_user)
            SELECT count(*) FILTER (WHERE dependency.deptype = 'o') AS owned_count,
                   count(*) FILTER (WHERE dependency.deptype = 'a')
                       AS acl_dependency_count
              FROM pg_shdepend AS dependency, login
             WHERE dependency.refclassid = 'pg_authid'::regclass
               AND dependency.refobjid = login.oid
            """
        ).fetchone()
    if row is None:
        raise ProductionPathQualificationError("database login role is absent")
    privileged = any(
        bool(row[field])
        for field in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    )
    expected_direct_grants = ("function:pg_catalog.pg_control_system():EXECUTE",)
    failures: list[str] = []
    if row["current_user_name"] != row["session_user_name"]:
        failures.append("session_role_changed")
    if row["rolcanlogin"] is not True or row["rolinherit"] is not True:
        failures.append("login_inheritance")
    if privileged:
        failures.append("privileged_login")
    if not memberships[expected] or sum(memberships.values()) != 1:
        failures.append("capability_membership")
    if inherited_roles != (expected,):
        failures.append("membership_closure")
    if direct_grants != expected_direct_grants:
        failures.append("direct_grants=" + "|".join(direct_grants))
    if shared_dependencies is None or shared_dependencies["owned_count"] != 0:
        failures.append("object_ownership")
    if shared_dependencies is None or shared_dependencies["acl_dependency_count"] != 1:
        failures.append("acl_dependency_closure")
    if failures:
        raise ProductionPathQualificationError(
            f"database login is not scoped exclusively to {expected}:"
            + ",".join(failures)
        )
    return {
        "current_user": str(row["current_user_name"]),
        "session_user": str(row["session_user_name"]),
        "superuser": bool(row["rolsuper"]),
        "createdb": bool(row["rolcreatedb"]),
        "createrole": bool(row["rolcreaterole"]),
        "replication": bool(row["rolreplication"]),
        "bypass_rls": bool(row["rolbypassrls"]),
        "can_login": bool(row["rolcanlogin"]),
        "inherit": bool(row["rolinherit"]),
        "capability_memberships": memberships,
        "inherited_roles": inherited_roles,
        "direct_grants": direct_grants,
        "owns_database_object": False,
        "direct_acl_dependency_count": 1,
        "exclusive_capability": expected,
    }


def _database_preflight(inputs: QualificationInputs) -> dict[str, object]:
    identities = {
        "capture": _postgres_identity(inputs.capture_connect),
        "analysis": _postgres_identity(inputs.analysis_connect),
        "audit": _postgres_identity(inputs.audit_connect),
    }
    expected = inputs.postgres_identity
    expected_identity = {
        "database_name": expected.database_name,
        "database_owner": expected.database_owner,
        "server_major": expected.server_major,
        "system_identifier": expected.system_identifier,
    }
    if any(identity != expected_identity for identity in identities.values()):
        raise ProductionPathQualificationError(
            "database identity differs from the approved disposable PostgreSQL cluster"
        )
    migrations = _migration_evidence(inputs.audit_connect, inputs.migration_directory)
    initial_counts = _application_table_counts(inputs.audit_connect)
    if initial_counts != {name: 0 for name in _APPLICATION_TABLES}:
        raise ProductionPathQualificationError(
            "disposable database contains application rows before qualification"
        )
    return {
        "identity": {**expected_identity, "all_connections_match": True},
        "migrations": migrations,
        "initial_table_counts": initial_counts,
    }


def _postgres_identity(connect: ConnectionFactory) -> dict[str, object]:
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            """
            SELECT current_database() AS database_name,
                   pg_get_userbyid(database.datdba) AS database_owner,
                   current_setting('server_version_num') AS server_version_num,
                   control.system_identifier::text AS system_identifier
              FROM pg_database AS database
              CROSS JOIN pg_control_system() AS control
             WHERE database.datname = current_database()
            """
        ).fetchone()
    if row is None:
        raise ProductionPathQualificationError("PostgreSQL identity is not observable")
    try:
        server_major = int(str(row["server_version_num"])) // 10_000
    except (TypeError, ValueError) as error:
        raise ProductionPathQualificationError(
            "PostgreSQL server version is not observable"
        ) from error
    return {
        "database_name": str(row["database_name"]),
        "database_owner": str(row["database_owner"]),
        "server_major": server_major,
        "system_identifier": str(row["system_identifier"]),
    }


def _migration_evidence(
    connect: ConnectionFactory, migration_directory: Path
) -> dict[str, object]:
    expected = _expected_migration_receipts(migration_directory)
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(
            "SELECT name, sha256 FROM schema_migration ORDER BY name"
        ).fetchall()
    actual = {str(row["name"]): str(row["sha256"]) for row in rows}
    names = tuple(actual)
    if (
        actual != expected
        or names != tuple(expected)
        or not names
        or names[-1] != EXPECTED_MIGRATION_HEAD
    ):
        raise ProductionPathQualificationError(
            "migration receipts do not exactly match the approved files through 0019"
        )
    return {
        "count": len(names),
        "first": names[0],
        "last": names[-1],
        "receipts": [
            {"name": str(row["name"]), "sha256": str(row["sha256"])} for row in rows
        ],
    }


def _expected_migration_receipts(directory: Path) -> dict[str, str]:
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ProductionPathQualificationError(
            "migration directory is unavailable"
        ) from error
    if not root.is_dir():
        raise ProductionPathQualificationError("migration path is not a directory")
    receipts: dict[str, str] = {}
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        try:
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or path.is_symlink():
                raise ProductionPathQualificationError(
                    "migration entries must be regular files"
                )
            if status.st_size > 4 * 1024 * 1024:
                raise ProductionPathQualificationError("migration file is too large")
            payload = path.read_bytes()
        except OSError as error:
            raise ProductionPathQualificationError(
                "migration file cannot be read"
            ) from error
        receipts[path.name] = hashlib.sha256(payload).hexdigest()
    if len(receipts) != 19 or tuple(receipts)[-1] != EXPECTED_MIGRATION_HEAD:
        raise ProductionPathQualificationError(
            "migration directory is not exactly the approved 0019 release"
        )
    return receipts


def _application_table_counts(connect: ConnectionFactory) -> dict[str, int]:
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
             ORDER BY table_name
            """
        ).fetchall()
        observed_tables = tuple(
            str(row["table_name"])
            for row in rows
            if row["table_name"] != "schema_migration"
        )
        if observed_tables != tuple(sorted(_APPLICATION_TABLES)):
            raise ProductionPathQualificationError(
                "public application table set differs from the approved schema"
            )
        counts: dict[str, int] = {}
        for table in _APPLICATION_TABLES:
            row = connection.execute(
                sql.SQL("SELECT count(*) AS row_count FROM public.{}").format(
                    sql.Identifier(table)
                )
            ).fetchone()
            if (
                row is None
                or isinstance(row["row_count"], bool)
                or not isinstance(row["row_count"], int)
            ):
                raise ProductionPathQualificationError(
                    "application table count is not observable"
                )
            counts[table] = row["row_count"]
    return counts


def _database_evidence(
    connect: ConnectionFactory,
    dwell_job_id: JobId,
    source_job_id: JobId,
    analysis_job_id: JobId,
    source_recording_id: RecordingId,
    dwell_recording_id: RecordingId,
    source_feature: FeatureSetRef,
    dwell_feature: FeatureSetRef,
    capture_plan: CapturePlanRef,
) -> dict[str, object]:
    with connect() as connection:
        jobs = connection.execute(
            """
            SELECT job_id, job_type, state, attempt, lease_generation, result_ref
              FROM job WHERE job_id = ANY(%s) ORDER BY job_id
            """,
            ([str(dwell_job_id), str(source_job_id), str(analysis_job_id)],),
        ).fetchall()
        ingress = connection.execute(
            """
            SELECT request_id, job_id, request_digest_algorithm,
                   request_digest_value, source_recording_id,
                   source_feature_set_id
              FROM dwell_request_ingress WHERE job_id = %s
            """,
            (str(dwell_job_id),),
        ).fetchone()
        recordings = connection.execute(
            """
            SELECT recording_id, manifest_digest_value
              FROM recording WHERE recording_id = ANY(%s) ORDER BY recording_id
            """,
            ([str(source_recording_id), str(dwell_recording_id)],),
        ).fetchall()
        features = connection.execute(
            """
            SELECT feature_set_id, analysis_run_id, recording_id,
                   input_recording_digest_algorithm,
                   input_recording_digest_value, bundle_digest_algorithm,
                   bundle_digest_value, observation_count, method_score_count
              FROM feature_set WHERE feature_set_id = ANY(%s)
              ORDER BY feature_set_id
            """,
            ([str(source_feature.feature_set_id), str(dwell_feature.feature_set_id)],),
        ).fetchall()
    counts = _application_table_counts(connect)
    expected_jobs = {
        str(dwell_job_id): (
            "dwell_capture",
            2,
            2,
            str(capture_plan.plan_id),
            str(capture_plan.plan_digest),
        ),
        str(source_job_id): (
            "recording_analysis",
            1,
            1,
            str(source_feature.feature_set_id),
            str(source_feature.bundle_ref.digest),
        ),
        str(analysis_job_id): (
            "recording_analysis",
            1,
            1,
            str(dwell_feature.feature_set_id),
            str(dwell_feature.bundle_ref.digest),
        ),
    }
    actual_jobs: dict[str, tuple[str, int, int, str, str]] = {}
    for row in jobs:
        result = row["result_ref"]
        if not isinstance(result, dict):
            raise ProductionPathQualificationError(
                "database terminal job result is absent"
            )
        actual_jobs[str(row["job_id"])] = (
            str(row["job_type"]),
            _database_int(row["attempt"], "attempt"),
            _database_int(row["lease_generation"], "lease_generation"),
            str(result.get("artifact_id")),
            f"{result.get('digest_algorithm')}:{result.get('digest_value')}",
        )
    if (
        ingress is None
        or len(jobs) != 3
        or actual_jobs != expected_jobs
        or any(row["state"] != JobState.SUCCEEDED.value for row in jobs)
        or len(recordings) != 2
        or {str(row["recording_id"]) for row in recordings}
        != {str(source_recording_id), str(dwell_recording_id)}
        or len(features) != 2
        or {str(row["feature_set_id"]) for row in features}
        != {str(source_feature.feature_set_id), str(dwell_feature.feature_set_id)}
        or counts != _FINAL_TABLE_COUNTS
    ):
        raise ProductionPathQualificationError("database receipt closure is incomplete")
    if (
        ingress["job_id"] != str(dwell_job_id)
        or ingress["request_id"] != DWELL_REQUEST_ID
        or ingress["source_recording_id"] != str(source_recording_id)
        or ingress["source_feature_set_id"] != str(source_feature.feature_set_id)
    ):
        raise ProductionPathQualificationError("dwell ingress lineage is not exact")
    return {
        "jobs": [_json_row(row) for row in jobs],
        "dwell_ingress": _json_row(ingress),
        "recordings": [_json_row(row) for row in recordings],
        "feature_sets": [_json_row(row) for row in features],
        "row_counts": counts,
        "closure_exact": True,
        "terminal_job_states_exact": True,
    }


def _json_row(row: Mapping[str, object]) -> dict[str, object]:
    return dict(row)


def _database_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductionPathQualificationError(f"database {field} is not an integer")
    return value


def _membership(
    connection: psycopg.Connection[dict[str, object]], subject: str, role: str
) -> bool:
    row = connection.execute(
        "SELECT pg_has_role(%s, %s, 'MEMBER') AS member", (subject, role)
    ).fetchone()
    if row is None or not isinstance(row["member"], bool):
        raise ProductionPathQualificationError("role membership is not observable")
    return row["member"]


def _feature_ref_evidence(ref: FeatureSetRef) -> dict[str, object]:
    return {
        "feature_set_id": str(ref.feature_set_id),
        "analysis_run_id": str(ref.analysis_run_id),
        "bundle_digest": str(ref.bundle_ref.digest),
        "bundle_byte_count": ref.bundle_ref.byte_count,
        "bundle_locator": ref.bundle_ref.locator,
    }


def _lease_evidence(lease: DwellRequestLease) -> dict[str, object]:
    return {
        "job_id": str(lease.job_id),
        "attempt": lease.attempt,
        "lease_generation": lease.lease_generation,
        "lease_token_digest": str(Digest.sha256(lease.lease_token.encode())),
        "lease_expires_utc_ns": int(lease.lease_expires_utc_ns),
    }


def _require_empty_local_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ProductionPathQualificationError("output root must be absolute")
    root = path.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ProductionPathQualificationError("output root must be absent or empty")
    ancestor = root
    while not ancestor.exists():
        parent = ancestor.parent
        if parent == ancestor:
            raise ProductionPathQualificationError(
                "output root has no observable existing ancestor"
            )
        ancestor = parent
    if not ancestor.is_dir():
        raise ProductionPathQualificationError(
            "output root ancestor is not a directory"
        )
    _require_non_nfs(ancestor)
    absent_paths: list[Path] = []
    candidate = root
    while candidate != ancestor:
        absent_paths.append(candidate)
        candidate = candidate.parent
    created_paths: list[Path] = []
    try:
        for candidate in reversed(absent_paths):
            try:
                candidate.mkdir(mode=0o700)
                created_paths.append(candidate)
            except FileExistsError:
                if not candidate.is_dir():
                    raise ProductionPathQualificationError(
                        "output root path became a non-directory"
                    ) from None
    except Exception:
        for created in reversed(created_paths):
            try:
                created.rmdir()
            except OSError:
                pass
        raise
    try:
        _require_non_nfs(root)
    except Exception:
        # Do not erase an operator-owned pre-existing empty root. A newly created
        # path is removed only while it remains empty after the failed recheck.
        for created in reversed(created_paths):
            try:
                created.rmdir()
            except OSError:
                pass
        raise
    return root


def _require_non_nfs(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    mount_id = _opened_mount_id(resolved)
    selected: tuple[str, str, str] | None = None
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        details = right.split()
        if len(fields) < 5 or len(details) < 2 or fields[0] != mount_id:
            continue
        mount = Path(_unescape_mount(fields[4]))
        try:
            resolved.relative_to(mount)
        except ValueError:
            raise ProductionPathQualificationError(
                "opened filesystem mount does not contain the qualification root"
            )
        selected = (str(mount), details[0], details[1])
        break
    if selected is None:
        raise ProductionPathQualificationError("filesystem mount is not observable")
    mount_point, filesystem_type, source = selected
    normalized_type = filesystem_type.lower()
    if normalized_type not in _LOCAL_FILESYSTEM_TYPES:
        raise ProductionPathQualificationError(
            "qualification root is not on an approved local filesystem"
        )
    return {
        "resolved_root": str(resolved),
        "filesystem_type": normalized_type,
        "mount_source": source,
        "mount_point": mount_point,
        "mount_id": mount_id,
        "approved_local_filesystem": True,
        "network_filesystem_observed": False,
    }


def _opened_mount_id(path: Path) -> str:
    flags = os.O_CLOEXEC | os.O_DIRECTORY
    flags |= os.O_PATH if hasattr(os, "O_PATH") else os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProductionPathQualificationError(
            "qualification root cannot be opened for mount verification"
        ) from error
    try:
        value = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
    except OSError as error:
        raise ProductionPathQualificationError(
            "opened filesystem mount identity is not observable"
        ) from error
    finally:
        os.close(descriptor)
    for line in value.splitlines():
        name, separator, observed = line.partition(":")
        if name == "mnt_id" and separator and observed.strip().isdigit():
            return observed.strip()
    raise ProductionPathQualificationError(
        "opened filesystem mount identity is invalid"
    )


def _unescape_mount(value: str) -> str:
    for encoded, plain in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
        value = value.replace(encoded, plain)
    return value


def _connect(dsn: str) -> ConnectionFactory:
    def connect() -> psycopg.Connection[dict[str, object]]:
        try:
            return psycopg.connect(dsn, row_factory=dict_row)
        except psycopg.Error:
            raise ProductionPathQualificationError(
                "database credential connection failed"
            ) from None

    return connect


def _secret(path: Path) -> str:
    if not path.is_absolute():
        raise ProductionPathQualificationError(
            "database credential path must be absolute"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProductionPathQualificationError(
            "database credential file cannot be opened safely"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid not in {0, os.geteuid()}
            or status.st_mode & 0o077
            or not status.st_mode & stat.S_IRUSR
            or status.st_size < 1
            or status.st_size > MAX_DSN_BYTES
        ):
            raise ProductionPathQualificationError(
                "database credential must be a bounded private regular file"
            )
        chunks: list[bytes] = []
        remaining = MAX_DSN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as error:
        raise ProductionPathQualificationError(
            "database credential file cannot be read safely"
        ) from error
    finally:
        os.close(descriptor)
    if len(payload) > MAX_DSN_BYTES or b"\x00" in payload:
        raise ProductionPathQualificationError("database credential file is invalid")
    try:
        value = payload.decode("utf-8").strip()
        psycopg.conninfo.conninfo_to_dict(value)
    except (UnicodeDecodeError, psycopg.Error):
        raise ProductionPathQualificationError(
            "database credential is not valid libpq connection information"
        ) from None
    if not value or "\n" in value or "\r" in value:
        raise ProductionPathQualificationError("database credential file is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cas-root", type=Path, required=True)
    parser.add_argument("--capture-dsn-file", type=Path, required=True)
    parser.add_argument("--analysis-dsn-file", type=Path, required=True)
    parser.add_argument("--audit-dsn-file", type=Path, required=True)
    parser.add_argument("--migration-directory", type=Path, required=True)
    parser.add_argument("--confirm-database-name", required=True)
    parser.add_argument("--confirm-database-owner", required=True)
    parser.add_argument("--confirm-system-identifier", required=True)
    parser.add_argument("--confirm-radio-serial", required=True)
    parser.add_argument("--confirm-clock-source", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        raise ProductionPathQualificationError("--live is required")
    if arguments.confirm_radio_serial != EXPECTED_SERIAL:
        raise ProductionPathQualificationError("radio serial confirmation differs")
    if arguments.confirm_clock_source != EXPECTED_CLOCK_CONFIRMATION:
        raise ProductionPathQualificationError("clock confirmation differs")
    report = run_qualification(
        QualificationInputs(
            arguments.output_root,
            arguments.source_cas_root,
            _connect(_secret(arguments.capture_dsn_file)),
            _connect(_secret(arguments.analysis_dsn_file)),
            _connect(_secret(arguments.audit_dsn_file)),
            V5RadioProvider(),
            lambda: verify_tx2_muted(EXPECTED_URI, EXPECTED_SERIAL),
            arguments.confirm_clock_source,
            PostgresIdentity(
                arguments.confirm_database_name,
                arguments.confirm_database_owner,
                arguments.confirm_system_identifier,
            ),
            arguments.migration_directory,
        )
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
