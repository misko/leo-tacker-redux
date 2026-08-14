from __future__ import annotations

import io
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from benchmark.starlink_e2e_calibration import (
    FrozenTrainCalibrationMember,
    calibrate_train_thresholds,
)
from benchmark.starlink_pilot_if import SUBCARRIER_SPACING_HZ
from benchmark.starlink_scan_fixture import (
    PairedStarlinkScanFixture,
    ReceiverPath,
    StarlinkPilotScanCase,
    generate_paired_starlink_scan_fixture,
)
from leo_flow.analysis.dataset import (
    DatasetCandidate,
    DatasetSnapshotBundle,
    DatasetSnapshotRef,
    DatasetSplit,
    DurableDatasetSnapshotRepository,
    DurableDetectorEvaluationRepository,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    carve_dataset,
    evaluate_detectors,
    freeze_dataset_snapshot,
)
from leo_flow.analysis.dataset.evaluation import DetectorEvaluationReport
from leo_flow.analysis.dataset.evaluation_persistence import (
    CatalogedEvaluation,
    EvaluationCatalogProjection,
)
from leo_flow.analysis.dataset.persistence import (
    CatalogedDatasetSnapshot,
    dataset_snapshot_projection,
)
from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DetectorSuiteConfig,
    DurableFeatureSetRepository,
    IndependentDetectorSuite,
    ThresholdRule,
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
)
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.application import DashboardProjectionStore
from leo_flow.capture import (
    CaptureIdentity,
    FakeV5PairedRadio,
    PlanCaptureEngine,
    PublicationReconciler,
    SQLiteLocalSpool,
    V5Refill,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import CapturePlan, GainMode, GainSetting
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    ContinuityStatus,
    RefillMetadata,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    EvaluationRunId,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorEvaluationView,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.ports import RadioDevice
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.dashboard import JsonRequest
from leo_flow.jobs import InMemoryJobLeaseRepository, JobLease, JobState
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    PreparedRecordingAnalysis,
    RecordingAnalysisJobPreparer,
)
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from leo_flow.storage import (
    FileSystemBlobStore,
    RootedSigMFRecordingStore,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from testkit import FakeClock

SAMPLE_RATE_HZ = 2_083_332
BANDWIDTH_HZ = 2_000_000
SAMPLE_COUNT = 4_096
RADIO = RadioId("radio_v5_starlink_closed_loop")
RECEIVERS = (
    ReceiverChainId("rx_v5_starlink_1"),
    ReceiverChainId("rx_v5_starlink_2"),
)
METHOD_IDS = (
    "coarse-energy@0.1.0",
    "paired-common-mode@0.1.0",
    "periodic-coherence@0.1.0",
)
SUITE_METHOD_ID = "independent-detector-suite@0.1.0"


@dataclass(frozen=True)
class _Case:
    name: str
    split_group: str
    split: DatasetSplit
    snr_db: float | None
    target_channel: int
    edge: Literal["lower", "upper"]
    pilots: tuple[int, ...]
    seed: int
    cfo_hz: float

    @property
    def present(self) -> bool:
        return self.snr_db is not None


@dataclass(frozen=True)
class _AnalyzedCase:
    case: _Case
    published: PublishedRecordingRef
    bundle: FeatureSetBundle
    feature_ref: FeatureSetRef
    truth: dict[str, Any]
    truth_ref: ObjectRef
    captured_utc_ns: int


@dataclass(frozen=True)
class _RunResult:
    evaluation: DetectorEvaluationReport
    evaluation_ref: DetectorEvaluationRef
    dataset: DatasetSnapshotBundle
    threshold_rule: ThresholdRule
    summary_bytes: bytes
    summary_ref: ObjectRef
    dashboard_bytes: bytes


class _FeatureCatalog:
    def __init__(self) -> None:
        self.entries: dict[str, CatalogedFeatureSet] = {}

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        del recording_ref, idempotency_key
        candidate = CatalogedFeatureSet(projection, bundle_ref)
        identity = projection.feature_set_id
        existing = self.entries.get(identity)
        if existing is not None and existing != candidate:
            raise RuntimeError("feature publication conflict")
        self.entries[identity] = candidate
        return candidate.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        candidate = self.entries.get(str(ref.feature_set_id))
        return candidate if candidate is not None and candidate.ref == ref else None


class _FeatureCommitter:
    def __init__(
        self,
        jobs: InMemoryJobLeaseRepository,
        repository: DurableFeatureSetRepository,
    ) -> None:
        self.jobs = jobs
        self.repository = repository
        self.ref: FeatureSetRef | None = None

    def commit(
        self, lease: JobLease, prepared: PreparedRecordingAnalysis
    ) -> ArtifactRef:
        self.ref = self.repository.publish(
            prepared.request,
            prepared.bundle,
            idempotency_key=f"recording-analysis:{lease.job_id}",
        )
        result = ArtifactRef(
            str(self.ref.feature_set_id),
            self.ref.bundle_ref.digest,
            prepared.bundle.schema,
        )
        self.jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


class _DatasetCatalog:
    def __init__(self) -> None:
        self.entries: dict[DatasetSnapshotRef, CatalogedDatasetSnapshot] = {}

    def publish(
        self,
        snapshot: DatasetSnapshotBundle,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> DatasetSnapshotRef:
        del idempotency_key
        candidate = CatalogedDatasetSnapshot(
            dataset_snapshot_projection(snapshot), bundle_ref
        )
        existing = self.entries.get(snapshot.ref)
        if existing is not None and existing != candidate:
            raise RuntimeError("dataset publication conflict")
        self.entries[snapshot.ref] = candidate
        return snapshot.ref

    def get(self, ref: DatasetSnapshotRef) -> CatalogedDatasetSnapshot | None:
        return self.entries.get(ref)


class _EvaluationCatalog:
    def __init__(self) -> None:
        self.entries: dict[DetectorEvaluationRef, CatalogedEvaluation] = {}

    def publish(
        self,
        projection: EvaluationCatalogProjection,
        report_object: ObjectRef,
        report: DetectorEvaluationReport,
        *,
        idempotency_key: str,
    ) -> DetectorEvaluationRef:
        del report, idempotency_key
        candidate = CatalogedEvaluation(projection, report_object)
        existing = next(
            (
                entry
                for ref, entry in self.entries.items()
                if ref.evaluation_id == candidate.ref.evaluation_id
            ),
            None,
        )
        if existing is not None and existing != candidate:
            raise RuntimeError("evaluation publication conflict")
        self.entries[candidate.ref] = candidate
        return candidate.ref

    def get(self, ref: DetectorEvaluationRef) -> CatalogedEvaluation | None:
        return self.entries.get(ref)


def _cases() -> tuple[_Case, ...]:
    groups = (
        ("background_101", DatasetSplit.TRAIN, 101),
        ("background_202", DatasetSplit.VALIDATION, 202),
        ("background_303", DatasetSplit.LOCKED_TEST, 303),
    )
    result: list[_Case] = []
    for group_index, (group, split, seed) in enumerate(groups):
        result.extend(
            (
                _Case(
                    f"{group}_null",
                    group,
                    split,
                    None,
                    1,
                    "lower",
                    (531, 532),
                    seed,
                    0.0,
                ),
                _Case(
                    f"{group}_snr_m12",
                    group,
                    split,
                    -12.0,
                    1 + group_index,
                    "lower",
                    (531, 532),
                    seed,
                    -20_000.0,
                ),
                _Case(
                    f"{group}_snr_m3",
                    group,
                    split,
                    -3.0,
                    2 + group_index,
                    "upper",
                    tuple(range(488, 496)),
                    seed,
                    15_000.0,
                ),
                _Case(
                    f"{group}_snr_p9",
                    group,
                    split,
                    9.0,
                    4 - group_index,
                    "lower",
                    tuple(range(528, 536)),
                    seed,
                    0.0,
                ),
            )
        )
    return tuple(result)


def _plan(case_index: int) -> CapturePlan:
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId(f"plan_closed_loop_case_{case_index:02d}"),
            radio_id=RADIO,
            receiver_chain_ids=RECEIVERS,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=float(SAMPLE_RATE_HZ),
            bandwidth_hz=float(BANDWIDTH_HZ),
            sample_count=SAMPLE_COUNT,
            edge_order="L",
            edge_order_draw_u32=0,
            arm_name="closed-loop-4096-sample-test-arm",
            hardware_block_samples=SAMPLE_COUNT,
        )
    )


def _fixture(case: _Case, plan: CapturePlan) -> PairedStarlinkScanFixture:
    noise_rms = 128.0
    signal_rms = (
        0.000_128 if case.snr_db is None else noise_rms * 10 ** (case.snr_db / 20)
    )
    return generate_paired_starlink_scan_fixture(
        plan,
        StarlinkPilotScanCase(
            signal_present=case.present,
            target_channels=(case.target_channel,),
            edge=case.edge,
            pilot_indices=case.pilots,
            seed_u64=case.seed,
            source_signal_rms_counts=signal_rms,
            cfo_hz=case.cfo_hz,
            receiver_paths=(
                ReceiverPath(ambient_noise_rms_counts=noise_rms),
                ReceiverPath(
                    integer_delay_samples=3,
                    gain_linear=0.8,
                    phase_offset_rad=0.35,
                    ambient_noise_rms_counts=noise_rms,
                ),
            ),
        ),
    )


def _metadata(case_index: int, segment_index: int) -> RefillMetadata:
    start = 1_700_000_000_000_000_000 + case_index * 1_000_000_000
    start += segment_index * 10_000_000
    first_sequence = (case_index * 8 + segment_index) * SAMPLE_COUNT
    return RefillMetadata(
        refill_index=0,
        segment_sample_offset=0,
        sample_count=SAMPLE_COUNT,
        stream_id=case_index * 8 + segment_index + 1,
        buffer_sequence=case_index * 8 + segment_index + 1,
        first_sample_sequence=first_sequence,
        monotonic_start_ns=10_000 + segment_index * 1_000,
        monotonic_end_ns=10_500 + segment_index * 1_000,
        utc_start_ns=start,
        utc_end_ns=start + 500,
        time_uncertainty_ns=50,
        gain_db_start=(40.0, 40.5),
        gain_db_end=(40.0, 40.5),
        rssi_db_start=(-50.0, -51.0),
        rssi_db_end=(-50.0, -51.0),
    )


def _capture_publish_analyze(
    root: Path,
    case: _Case,
    case_index: int,
    blobs: FileSystemBlobStore,
    recording_catalog: InMemoryRecordingCatalog,
    feature_repository: DurableFeatureSetRepository,
    projections: DashboardProjectionStore,
) -> _AnalyzedCase:
    plan = _plan(case_index)
    fixture = _fixture(case, plan)
    assert fixture == _fixture(case, plan)
    requests = plan.activities[0].segments
    scripts = {
        segment.segment_id: (
            V5Refill(
                fixture.payload_for(segment.segment_id),
                _metadata(case_index, segment_index),
            ),
        )
        for segment_index, segment in enumerate(requests)
    }
    clock = FakeClock(1_700_000_000_000_000_000 + case_index * 1_000_000_000)
    radio = FakeV5PairedRadio(
        RADIO,
        RECEIVERS,
        scripts,
        CaptureProvenance("v5", "closed-loop", "0.25", "v3", "metadata=1"),
        continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        clock=clock,
    )
    capture_root = root / "capture" / f"case_{case_index:02d}"
    recording_id = RecordingId(f"rec_closed_loop_case_{case_index:02d}")
    spool = SQLiteLocalSpool(
        capture_root / "capture.sqlite3",
        capture_root / "recordings",
        id_factory=lambda: recording_id,
        now_ns=clock.now_utc_ns,
    )
    completed = PlanCaptureEngine(
        CaptureIdentity(
            StationId("station_closed_loop"),
            "synthetic-v5-radio",
            "deterministic-test-clock",
            HardwareSnapshotId("hw_closed_loop_v5"),
            "starlink-closed-loop-e2e",
        ),
        clock=clock,
    ).execute(plan, cast(RadioDevice, radio), SigMFRecordingWriter(), spool)
    assert len(completed.manifest.segments) == 8
    assert all(
        item.sample_count == SAMPLE_COUNT for item in completed.manifest.segments
    )
    assert [item.segment_id for item in completed.manifest.segments] == [
        item.segment_id for item in requests
    ]
    manifest_bytes = canonical_json_bytes(completed.manifest)
    assert case.name.encode() not in manifest_bytes
    assert b"background_" not in manifest_bytes
    assert b"snr_" not in manifest_bytes
    assert b"signal_present" not in manifest_bytes
    local = RootedSigMFRecordingStore(capture_root / "recordings")
    reconciled = PublicationReconciler(
        spool,
        RecordingPublisherAdapter(local, blobs, recording_catalog),
        local,
    ).reconcile()
    assert (reconciled.published, reconciled.cleaned, reconciled.deferred) == (1, 1, 0)
    published = recording_catalog.get(str(recording_id))
    assert published is not None

    truth_ref = blobs.put(
        io.BytesIO(fixture.truth_json),
        expected_digest=Digest.sha256(fixture.truth_json),
        expected_bytes=len(fixture.truth_json),
        media_type="application/json",
        format_id="paired-starlink-scan-truth-v1",
        idempotency_key=f"truth:{case.name}",
    )
    assert blobs.head(truth_ref).verified

    config = DetectorSuiteConfig(
        window_samples=SAMPLE_COUNT,
        stride_samples=SAMPLE_COUNT,
        periodic_lag_samples=9,
        max_pair_delay_samples=4,
        clip_threshold_abs=2048,
    )
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    submitted = RecordingAnalysisSubmissionService(jobs).submit(
        RecordingAnalysisSubmission(
            published,
            detector_suite_algorithm_ref(),
            detector_suite_config_ref(config),
            (),
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
    )
    assert submitted.request.dependency_refs == ()
    committer = _FeatureCommitter(jobs, feature_repository)
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            SigMFRecordingObjectReader(blobs),
            IndependentDetectorSuite(
                config,
                AnalysisExecutionContext(
                    "closed-loop-detector-suite",
                    "0.1.0",
                    "closed-loop-fixture",
                    Digest.sha256(b"closed-loop-environment"),
                    UtcNs(100),
                    UtcNs(101),
                    "deterministic-test-host",
                ),
            ),
        ),
        committer,
        worker_id="closed-loop-worker",
        lease_ttl_s=30,
    )
    assert worker.process_one_job()
    assert jobs.snapshot(submitted.job_id).state is JobState.SUCCEEDED
    assert committer.ref is not None
    with feature_repository.open(committer.ref) as opened:
        bundle = opened.bundle()
    assert len(bundle.method_scores) == 8 * len(METHOD_IDS)
    assert {
        f"{score.method_id}@{score.method_version}" for score in bundle.method_scores
    } == set(METHOD_IDS)
    with SigMFRecordingObjectReader(blobs).open(published.recording_object) as view:
        for segment in completed.manifest.segments:
            continuity = view.continuity(segment.segment_id)
            assert continuity is not None
            assert continuity.status is ContinuityStatus.VERIFIED_CONTIGUOUS
            raw = view.read_iq_bytes(segment.segment_id, 0, SAMPLE_COUNT)
            expected = next(
                item["paired_ci16_sha256"]
                for item in fixture.truth["segments"]
                if item["segment_id"] == str(segment.segment_id)
            )
            assert Digest.sha256(raw).value == expected
    projections.project_recording(
        completed.manifest,
        recording_object_available=True,
        analysis_state="complete",
    )
    projections.project_features(bundle)
    return _AnalyzedCase(
        case,
        published,
        bundle,
        committer.ref,
        fixture.truth,
        truth_ref,
        int(completed.manifest.capture_started_utc_ns),
    )


def _segments_by_coordinates(
    truth: dict[str, Any],
) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (int(segment["channel"]), str(segment["edge"])): segment
        for segment in truth["segments"]
    }


def _verify_frozen_background_lineage(analyzed: tuple[_AnalyzedCase, ...]) -> None:
    null_by_group = {
        item.case.split_group: item for item in analyzed if not item.case.present
    }
    for item in analyzed:
        if not item.case.present:
            continue
        null_segments = _segments_by_coordinates(
            null_by_group[item.case.split_group].truth
        )
        for segment in item.truth["segments"]:
            base = null_segments[(int(segment["channel"]), str(segment["edge"]))]
            for injected_rx, base_rx in zip(
                segment["receivers"], base["receivers"], strict=True
            ):
                assert injected_rx["noise_seed_u64"] == base_rx["noise_seed_u64"]
                assert (
                    injected_rx["base_noise_ci16_sha256"]
                    == base_rx["base_noise_ci16_sha256"]
                    == base_rx["ci16_sha256"]
                )
            if not segment["expected_signal_present"]:
                assert segment["paired_ci16_sha256"] == base["paired_ci16_sha256"]


def _assert_predeclared_heldout_signal_effect(
    analyzed: tuple[_AnalyzedCase, ...],
) -> None:
    """Check a fixed physical invariant without fitting on held-out data."""

    null_by_group = {
        item.case.split_group: item for item in analyzed if not item.case.present
    }
    heldout = tuple(
        item
        for item in analyzed
        if item.case.split is not DatasetSplit.TRAIN and item.case.snr_db == 9.0
    )
    assert len(heldout) == 2
    method = "paired-common-mode@0.1.0"
    for positive in heldout:
        null = null_by_group[positive.case.split_group]
        target = next(
            segment
            for segment in positive.truth["segments"]
            if segment["expected_signal_present"]
        )
        null_segment = _segments_by_coordinates(null.truth)[
            (int(target["channel"]), str(target["edge"]))
        ]
        positive_score = next(
            score.score
            for score in positive.bundle.method_scores
            if f"{score.method_id}@{score.method_version}" == method
            and str(score.segment_id) == target["segment_id"]
        )
        null_score = next(
            score.score
            for score in null.bundle.method_scores
            if f"{score.method_id}@{score.method_version}" == method
            and str(score.segment_id) == null_segment["segment_id"]
        )
        # A strong signal common to both receivers must materially exceed the
        # matched independent-noise coherence. The method and margin are fixed.
        assert positive_score >= null_score + 0.5


def _make_candidates(
    analyzed: tuple[_AnalyzedCase, ...],
) -> tuple[DatasetCandidate, ...]:
    _verify_frozen_background_lineage(analyzed)
    null_by_group = {
        item.case.split_group: item for item in analyzed if not item.case.present
    }
    independent = (SUITE_METHOD_ID,) + METHOD_IDS
    result: list[DatasetCandidate] = []
    for item in analyzed:
        base = null_by_group[item.case.split_group]
        evidence = LabelEvidence(
            LabelSource.INJECTED,
            item.truth_ref.digest,
            "starlink-closed-loop-fixture-v1",
            item.captured_utc_ns,
            independent,
            uncertainty=(
                ("scope", "coded-edge-pilot-approximation"),
                ("channel", str(item.case.target_channel)),
                ("edge", item.case.edge),
                (
                    "injection_role",
                    "signal-injection" if item.case.present else "zero-signal-control",
                ),
            ),
            base_recording_digest=base.published.recording_object.identity_digest(),
            injection_spec_digest=item.truth_ref.digest,
        )
        result.append(
            DatasetCandidate(
                feature_set_id=str(item.feature_ref.feature_set_id),
                feature_set_digest=item.feature_ref.bundle_ref.digest,
                recording_id=str(item.published.recording_object.recording_id),
                split_group_id=item.case.split_group,
                captured_utc_ns=item.captured_utc_ns,
                radio_id=str(RADIO),
                lnb_ids=("synthetic-if-no-lnb",),
                observation_mode="synthetic-pilot-scan-4096-test-arm",
                sample_rate_hz=SAMPLE_RATE_HZ,
                gain_mode="agc",
                gain_db=None,
                satellite_id=None,
                truth=TruthLabel(
                    item.case.present,
                    LabelSource.INJECTED,
                    (evidence,),
                    confidence=1.0,
                ),
                derived_from_recording_id=(
                    str(base.published.recording_object.recording_id)
                    if item.case.present
                    else None
                ),
            )
        )
    return tuple(result)


def _evaluation_view(
    ref: DetectorEvaluationRef, report: DetectorEvaluationReport
) -> DetectorEvaluationView:
    methods = tuple(
        DetectorMethodSplitSummary(
            method.method_id,
            split.split,
            method.threshold,
            method.score_semantics,
            split.feature_set_count,
            split.feature_set_present_count,
            split.union_window_count,
            split.present_window_count,
            split.missing_window_count,
            split.firing_count,
            split.truth.true_positive,
            split.truth.false_positive,
            split.truth.true_negative,
            split.truth.false_negative,
            split.truth.scored_prediction_count,
            split.truth.missing_prediction_count,
        )
        for method in report.methods
        for split in method.by_split
    )
    return DetectorEvaluationView(
        ref,
        report.dataset_snapshot_id,
        report.dataset_snapshot_digest,
        report.feature_membership_digest,
        report.threshold_rule_id,
        report.threshold_rule_digest,
        report.threshold_calibration_dataset_id,
        report.threshold_calibration_split,
        len(report.methods),
        report.overall_association.union_window_count,
        report.warnings,
        methods,
    )


def _segment_report(
    analyzed: tuple[_AnalyzedCase, ...], rule: ThresholdRule
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    thresholds = dict(rule.thresholds)
    counts: dict[str, Counter[str]] = {method: Counter() for method in METHOD_IDS}
    snr_trials: dict[str, dict[str, list[bool]]] = {
        method: defaultdict(list) for method in METHOD_IDS
    }
    achieved_snr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    frequency_errors: list[float] = []
    frequency_bin_hz = SAMPLE_RATE_HZ / SAMPLE_COUNT
    for item in analyzed:
        target_ids = set(item.truth["expected_target_segment_ids"])
        score_by = {
            (f"{score.method_id}@{score.method_version}", str(score.segment_id)): score
            for score in item.bundle.method_scores
        }
        for method in METHOD_IDS:
            target_fired = False
            for segment in item.truth["segments"]:
                target = bool(segment["expected_signal_present"])
                fired = (
                    score_by[(method, segment["segment_id"])].score
                    >= thresholds[method]
                )
                counts[method][
                    "tp"
                    if target and fired
                    else "fn"
                    if target
                    else "fp"
                    if fired
                    else "tn"
                ] += 1
                target_fired = target_fired or (target and fired)
            if item.case.present:
                assert len(target_ids) == 1
                snr_trials[method][str(item.case.snr_db)].append(target_fired)
        if item.case.present:
            target = next(
                segment
                for segment in item.truth["segments"]
                if segment["expected_signal_present"]
            )
            nominal_snr = str(item.case.snr_db)
            achieved_snr[nominal_snr].append(
                {
                    "truth_digest": str(item.truth_ref.digest),
                    "receiver_chain_ids": [
                        receiver["receiver_chain_id"]
                        for receiver in target["receivers"]
                    ],
                    "requested_receiver_snr_db": [
                        receiver["requested_snr_db"] for receiver in target["receivers"]
                    ],
                    "achieved_prequantization_snr_db": [
                        receiver["achieved_prequantization_snr_db"]
                        for receiver in target["receivers"]
                    ],
                }
            )
            expected_offsets = target["expected_pilot_local_offsets_hz"]
            for observation in item.bundle.observations:
                if (
                    observation.method_id == "coarse-energy"
                    and str(observation.segment_id) == target["segment_id"]
                ):
                    assert observation.frequency_offset_hz is not None
                    frequency_errors.append(
                        min(
                            abs(observation.frequency_offset_hz - expected)
                            for expected in expected_offsets
                        )
                    )
    segment_summary = {
        method: {
            "true_positive": counts[method]["tp"],
            "false_positive": counts[method]["fp"],
            "true_negative": counts[method]["tn"],
            "false_negative": counts[method]["fn"],
            "target_window_count": counts[method]["tp"] + counts[method]["fn"],
            "non_target_window_count": counts[method]["fp"] + counts[method]["tn"],
        }
        for method in METHOD_IDS
    }
    snr_summary = {
        method: {
            snr: {
                "detected": sum(values),
                "trials": len(values),
                "fraction": sum(values) / len(values),
            }
            for snr, values in sorted(
                snr_trials[method].items(), key=lambda x: float(x[0])
            )
        }
        for method in METHOD_IDS
    }
    frequency_summary = {
        "definition": "nearest injected coded-pilot center including CFO",
        "interpretation": (
            "candidate lies inside known coded-pilot occupied support; this is not "
            "tone-center or CFO estimator accuracy"
        ),
        "fft_bin_hz": frequency_bin_hz,
        "accepted_occupied_support_error_hz": (
            SUBCARRIER_SPACING_HZ / 2 + frequency_bin_hz
        ),
        "observation_count": len(frequency_errors),
        "maximum_error_hz": max(frequency_errors),
        "mean_error_hz": math.fsum(frequency_errors) / len(frequency_errors),
    }
    achieved_snr_summary = {
        "nominal_axis": "RX1/source requested SNR in dB",
        "cases_by_nominal_snr_db": {
            snr: cases
            for snr, cases in sorted(
                achieved_snr.items(), key=lambda item: float(item[0])
            )
        },
    }
    return segment_summary, snr_summary, achieved_snr_summary, frequency_summary


def _run_pipeline(root: Path) -> _RunResult:
    blobs = FileSystemBlobStore(root / "cas")
    recording_catalog = InMemoryRecordingCatalog()
    feature_repository = DurableFeatureSetRepository(blobs, _FeatureCatalog())
    projections = DashboardProjectionStore()
    analyzed = tuple(
        _capture_publish_analyze(
            root,
            case,
            index,
            blobs,
            recording_catalog,
            feature_repository,
            projections,
        )
        for index, case in enumerate(_cases())
    )
    _assert_predeclared_heldout_signal_effect(analyzed)
    candidates = _make_candidates(analyzed)
    assignments = {item.case.split_group: item.case.split for item in analyzed}
    carved = carve_dataset(
        candidates,
        group_partitions=assignments,
        evaluated_method_id=SUITE_METHOD_ID,
        require_promotion=True,
    )
    dataset = freeze_dataset_snapshot(
        carved,
        candidates,
        (item.feature_ref for item in analyzed),
        selection_spec="closed-loop-v1:three-frozen-background-groups",
        selection_cutoff_utc_ns=UtcNs(max(item.captured_utc_ns for item in analyzed)),
    )
    dataset_repository = DurableDatasetSnapshotRepository(blobs, _DatasetCatalog())
    dataset_ref = dataset_repository.publish(
        dataset, idempotency_key="dataset:starlink-closed-loop"
    )
    assert dataset_repository.get(dataset_ref) == dataset

    train = tuple(
        FrozenTrainCalibrationMember(
            item.bundle, item.case.present, item.case.split_group
        )
        for item in analyzed
        if item.case.split is DatasetSplit.TRAIN
    )
    threshold_rule = calibrate_train_thresholds(train, expected_method_ids=METHOD_IDS)
    assert threshold_rule == calibrate_train_thresholds(
        tuple(reversed(train)), expected_method_ids=tuple(reversed(METHOD_IDS))
    )
    feature_sets = {
        str(item.feature_ref.feature_set_id): item.bundle for item in analyzed
    }
    evaluation = evaluate_detectors(dataset, feature_sets, threshold_rule)
    evaluation_repository = DurableDetectorEvaluationRepository(
        blobs, _EvaluationCatalog()
    )
    evaluation_ref = evaluation_repository.publish(
        EvaluationRunId("erun_starlink_closed_loop_v1"),
        evaluation,
        idempotency_key="evaluation:starlink-closed-loop",
    )
    with evaluation_repository.open(evaluation_ref) as opened:
        assert opened.report == evaluation

    projections.project_evaluation(_evaluation_view(evaluation_ref, evaluation))
    dashboard = projections.json_application().handle(
        JsonRequest("GET", f"/api/evaluations/{evaluation_ref.evaluation_id}", {})
    )
    assert dashboard.status == 200
    dashboard_doc = json.loads(dashboard.body)
    assert dashboard_doc["evaluation_id"] == str(evaluation_ref.evaluation_id)
    assert dashboard_doc["method_count"] == len(METHOD_IDS)
    assert len(dashboard_doc["methods"]) == len(METHOD_IDS) * 3
    assert dashboard_doc["report_object"]["digest"]["value"] == evaluation.digest.value
    expected_dashboard_rows = {
        (method.method_id, split.split): {
            "threshold": method.threshold,
            "score_semantics": method.score_semantics,
            "coverage": {
                "feature_set_count": split.feature_set_count,
                "feature_set_present_count": split.feature_set_present_count,
                "union_window_count": split.union_window_count,
                "present_window_count": split.present_window_count,
                "missing_window_count": split.missing_window_count,
                "scored_prediction_count": split.truth.scored_prediction_count,
                "missing_prediction_count": split.truth.missing_prediction_count,
            },
            "firing_count": split.firing_count,
            "confusion": {
                "true_positive": split.truth.true_positive,
                "false_positive": split.truth.false_positive,
                "true_negative": split.truth.true_negative,
                "false_negative": split.truth.false_negative,
            },
        }
        for method in evaluation.methods
        for split in method.by_split
    }
    assert {
        (row["method_id"], row["split"]): {
            "threshold": row["threshold"],
            "score_semantics": row["score_semantics"],
            "coverage": row["coverage"],
            "firing_count": row["firing_count"],
            "confusion": row["confusion"],
        }
        for row in dashboard_doc["methods"]
    } == expected_dashboard_rows

    (
        segment_summary,
        snr_summary,
        achieved_snr_summary,
        frequency_summary,
    ) = _segment_report(analyzed, threshold_rule)
    assert all(
        summary["target_window_count"] == 9 for summary in segment_summary.values()
    )
    assert all(
        summary["non_target_window_count"] == 87 for summary in segment_summary.values()
    )
    assert frequency_summary["observation_count"] == 18
    # The published pilot coefficient changes every OFDM symbol, so a whole-scan
    # rectangular FFT estimates energy within the subcarrier's occupied support;
    # it is not a tone-center or CFO estimator.
    assert (
        frequency_summary["maximum_error_hz"]
        <= frequency_summary["accepted_occupied_support_error_hz"]
    )
    summary_bytes = canonical_json_bytes(
        {
            "schema": "leo-flow.starlink-closed-loop-e2e-report/v1",
            "scientific_scope": (
                "deterministic coded edge-pilot IF approximation with paired receiver "
                "impairments; not a complete Starlink downlink, independent trial set, "
                "or over-the-air qualification"
            ),
            "persistence_scope": (
                "filesystem CAS survives adapter reconstruction; in-memory catalogs "
                "exercise catalog semantics while PostgreSQL durability is covered by "
                "its separate adapter integration suite"
            ),
            "test_arm": {
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "bandwidth_hz": BANDWIDTH_HZ,
                "sample_count_per_segment": SAMPLE_COUNT,
                "segment_count_per_scan": 8,
                "production_duration_sample_count": 262_144,
                "duration_matches_production": False,
                "case_count": len(analyzed),
                "positive_case_count": sum(item.case.present for item in analyzed),
                "null_case_count": sum(not item.case.present for item in analyzed),
            },
            "artifact_refs": {
                "recordings": [item.published.recording_object for item in analyzed],
                "truth": [item.truth_ref for item in analyzed],
                "feature_sets": [item.feature_ref for item in analyzed],
                "dataset_snapshot": dataset_ref,
                "evaluation": evaluation_ref,
            },
            "threshold_rule": threshold_rule,
            "recording_level_methods": evaluation.methods,
            "segment_level_methods": segment_summary,
            "threshold_basis": (
                "TRAIN per-recording maximum balanced-accuracy rule applied unchanged "
                "to segment/window scores; not a segment-calibrated operating point"
            ),
            "per_nominal_rx1_snr_target_detection": snr_summary,
            "achieved_snr_truth": achieved_snr_summary,
            "coarse_energy_frequency_error": frequency_summary,
            "firing_covariance": evaluation.overall_association,
            "dashboard": dashboard_doc,
        }
    )
    summary_ref = blobs.put(
        io.BytesIO(summary_bytes),
        expected_digest=Digest.sha256(summary_bytes),
        expected_bytes=len(summary_bytes),
        media_type="application/json",
        format_id="starlink-closed-loop-e2e-report-v1",
        idempotency_key="report:starlink-closed-loop",
    )
    with blobs.open(summary_ref) as stream:
        assert stream.read() == summary_bytes
    restarted_blobs = FileSystemBlobStore(root / "cas")
    with restarted_blobs.open(summary_ref) as stream:
        assert stream.read() == summary_bytes
    with restarted_blobs.open(evaluation_ref.report_object) as stream:
        assert Digest.sha256(stream.read()) == evaluation_ref.report_digest
    for item in analyzed:
        assert restarted_blobs.head(item.truth_ref).verified
        assert restarted_blobs.head(
            item.published.recording_object.data_object
        ).verified
        assert restarted_blobs.head(
            item.published.recording_object.metadata_object
        ).verified
    return _RunResult(
        evaluation,
        evaluation_ref,
        dataset,
        threshold_rule,
        summary_bytes,
        summary_ref,
        dashboard.body,
    )


def test_starlink_signal_scan_store_analyze_evaluate_and_report_is_reproducible(
    tmp_path: Path,
) -> None:
    first = _run_pipeline(tmp_path / "first")
    second = _run_pipeline(tmp_path / "second")

    assert first.dataset.promoted
    assert first.evaluation.overall_association.method_ids == METHOD_IDS
    assert first.evaluation_ref.report_digest == first.evaluation.digest
    assert first.summary_ref.digest == Digest.sha256(first.summary_bytes)
    assert first.threshold_rule == second.threshold_rule
    assert first.dataset.snapshot_digest == second.dataset.snapshot_digest
    assert first.evaluation.digest == second.evaluation.digest
    assert first.evaluation_ref == second.evaluation_ref
    assert first.summary_bytes == second.summary_bytes
    assert first.dashboard_bytes == second.dashboard_bytes
