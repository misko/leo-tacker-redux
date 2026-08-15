"""Bounded offline durable E2E harness for paired synthetic Starlink-like IQ.

The harness uses only public capture, storage, recording-analysis, dataset, and
evaluation interfaces.  It never opens a radio, database, or network service.
All generated IQ is bounded and written beneath an explicitly selected local
workspace before content-addressed handoff and spool cleanup.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from benchmark.starlink_e2e_calibration import (
    FrozenTrainCalibrationMember,
    calibrate_train_thresholds,
)
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
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
)
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.capture import (
    CaptureIdentity,
    FakeV5PairedRadio,
    PlanCaptureEngine,
    PublicationReconciler,
    SpoolState,
    SQLiteLocalSpool,
    V5Refill,
)
from leo_flow.capture.errors import ContinuityError, SampleCountError
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import CapturePlan, GainMode, GainSetting
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
)
from leo_flow.contracts.core import (
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
from leo_flow.contracts.evaluation import DetectorEvaluationRef
from leo_flow.contracts.features import (
    FeatureSetBundle,
    FeatureSetRef,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.ports import RadioDevice
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.storage import (
    FileSystemBlobReader,
    FileSystemBlobStore,
    RootedSigMFRecordingStore,
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter

SCHEMA = "leo-flow.starlink-durable-e2e-report/v1"
SAMPLE_RATE_HZ = 2_083_332
BANDWIDTH_HZ = 2_000_000
SAMPLE_COUNT = 4_096
SEGMENTS_PER_RECORDING = 8
MAX_CASES = 6
MAX_DETECTOR_WINDOWS = MAX_CASES * SEGMENTS_PER_RECORDING
MAX_GENERATED_IQ_BYTES = MAX_DETECTOR_WINDOWS * SAMPLE_COUNT * 8
MAX_DURABLE_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 1 * 1024 * 1024
MAX_RUNTIME_SECONDS = 120.0
RADIO = RadioId("radio_offline_durable_e2e")
RECEIVERS = (
    ReceiverChainId("rx_offline_durable_1"),
    ReceiverChainId("rx_offline_durable_2"),
)
METHOD_IDS = (
    "coarse-energy@0.1.0",
    "paired-common-mode@0.1.0",
    "periodic-coherence@0.1.0",
)
SUITE_METHOD_ID = "independent-detector-suite@0.1.0"
CONFIG = DetectorSuiteConfig(
    window_samples=SAMPLE_COUNT,
    stride_samples=SAMPLE_COUNT,
    periodic_lag_samples=9,
    max_pair_delay_samples=4,
    clip_threshold_abs=2048,
)


@dataclass(frozen=True)
class HarnessResult:
    report: Mapping[str, Any]
    report_bytes: bytes
    report_object: ObjectRef
    elapsed_seconds: float


@dataclass(frozen=True)
class _Clock:
    utc_ns: int
    monotonic_ns: int = 100

    def now_utc_ns(self) -> int:
        return self.utc_ns

    def now_monotonic_ns(self) -> int:
        return self.monotonic_ns


@dataclass(frozen=True)
class _Case:
    case_id: str
    group_id: str
    split: DatasetSplit
    signal_present: bool
    seed_u64: int
    target_channel: int


@dataclass(frozen=True)
class _Analyzed:
    case: _Case
    published: PublishedRecordingRef
    truth: Mapping[str, Any]
    truth_ref: ObjectRef
    bundle: FeatureSetBundle
    feature_ref: FeatureSetRef
    captured_utc_ns: int


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
        value = CatalogedFeatureSet(projection, bundle_ref)
        prior = self.entries.get(projection.feature_set_id)
        if prior is not None and prior != value:
            raise RuntimeError("feature publication conflict")
        self.entries[projection.feature_set_id] = value
        return value.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        value = self.entries.get(str(ref.feature_set_id))
        return value if value is not None and value.ref == ref else None


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
        value = CatalogedDatasetSnapshot(
            dataset_snapshot_projection(snapshot), bundle_ref
        )
        prior = self.entries.get(snapshot.ref)
        if prior is not None and prior != value:
            raise RuntimeError("dataset publication conflict")
        self.entries[snapshot.ref] = value
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
        value = CatalogedEvaluation(projection, report_object)
        prior = next(
            (
                item
                for ref, item in self.entries.items()
                if ref.evaluation_id == value.ref.evaluation_id
            ),
            None,
        )
        if prior is not None and prior != value:
            raise RuntimeError("evaluation publication conflict")
        self.entries[value.ref] = value
        return value.ref

    def get(self, ref: DetectorEvaluationRef) -> CatalogedEvaluation | None:
        return self.entries.get(ref)


def _cases() -> tuple[_Case, ...]:
    groups = (
        ("group_train_101", DatasetSplit.TRAIN, 101, 1),
        ("group_validation_202", DatasetSplit.VALIDATION, 202, 2),
        ("group_locked_303", DatasetSplit.LOCKED_TEST, 303, 3),
    )
    return tuple(
        _Case(
            f"{group}_{'signal' if present else 'null'}",
            group,
            split,
            present,
            seed,
            channel,
        )
        for group, split, seed, channel in groups
        for present in (False, True)
    )


def _plan(index: int, *, suffix: str = "") -> CapturePlan:
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId(f"plan_durable_e2e_{index:02d}{suffix}"),
            radio_id=RADIO,
            receiver_chain_ids=RECEIVERS,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=float(SAMPLE_RATE_HZ),
            bandwidth_hz=float(BANDWIDTH_HZ),
            sample_count=SAMPLE_COUNT,
            edge_order="L",
            edge_order_draw_u32=0,
            arm_name="offline-durable-e2e-4096",
            hardware_block_samples=SAMPLE_COUNT,
        )
    )


def _fixture(case: _Case, plan: CapturePlan) -> PairedStarlinkScanFixture:
    noise_rms = 128.0
    return generate_paired_starlink_scan_fixture(
        plan,
        StarlinkPilotScanCase(
            signal_present=case.signal_present,
            target_channels=(case.target_channel,),
            edge="lower",
            pilot_indices=tuple(range(528, 536)),
            seed_u64=case.seed_u64,
            source_signal_rms_counts=(noise_rms * 10 ** (6.0 / 20)),
            cfo_hz=15_000.0,
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


def _metadata(
    case_index: int,
    segment_index: int,
    *,
    refill_index: int = 0,
    offset: int = 0,
    count: int = SAMPLE_COUNT,
    first_sequence_delta: int = 0,
) -> RefillMetadata:
    stream = case_index * SEGMENTS_PER_RECORDING + segment_index + 1
    first = (stream - 1) * SAMPLE_COUNT + offset + first_sequence_delta
    start = 1_750_000_000_000_000_000 + case_index * 1_000_000_000
    start += segment_index * 10_000_000 + refill_index * 1_000
    return RefillMetadata(
        refill_index=refill_index,
        segment_sample_offset=offset,
        sample_count=count,
        stream_id=stream,
        buffer_sequence=stream * 10 + refill_index,
        first_sample_sequence=first,
        monotonic_start_ns=10_000 + segment_index * 10 + refill_index,
        monotonic_end_ns=10_001 + segment_index * 10 + refill_index,
        utc_start_ns=start,
        utc_end_ns=start + 1,
        time_uncertainty_ns=50,
        gain_db_start=(40.0, 40.5),
        gain_db_end=(40.0, 40.5),
        rssi_db_start=(-50.0, -51.0),
        rssi_db_end=(-50.0, -51.0),
    )


def _radio_scripts(
    fixture: PairedStarlinkScanFixture, plan: CapturePlan, case_index: int
) -> dict[Any, tuple[V5Refill, ...]]:
    return {
        request.segment_id: (
            V5Refill(
                fixture.payload_for(request.segment_id),
                _metadata(case_index, segment_index),
            ),
        )
        for segment_index, request in enumerate(plan.activities[0].segments)
    }


def _capture_analyze(
    workspace: Path,
    case: _Case,
    index: int,
    blobs: FileSystemBlobStore,
    recording_catalog: InMemoryRecordingCatalog,
    features: DurableFeatureSetRepository,
) -> _Analyzed:
    plan = _plan(index)
    fixture = _fixture(case, plan)
    scripts = _radio_scripts(fixture, plan, index)
    clock = _Clock(1_750_000_000_000_000_000 + index * 1_000_000_000)
    radio = FakeV5PairedRadio(
        RADIO,
        RECEIVERS,
        scripts,
        CaptureProvenance("v5", "offline-harness", "0.25", "v3", "metadata=1"),
        continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        clock=clock,
    )
    capture_root = workspace / "capture" / f"case_{index:02d}"
    recording_id = RecordingId(f"rec_durable_e2e_{index:02d}")
    spool = SQLiteLocalSpool(
        capture_root / "capture.sqlite3",
        capture_root / "recordings",
        id_factory=lambda: recording_id,
        now_ns=clock.now_utc_ns,
    )
    completed = PlanCaptureEngine(
        CaptureIdentity(
            StationId("station_offline_durable"),
            "synthetic-no-hardware",
            "deterministic-clock",
            HardwareSnapshotId("hw_offline_durable"),
            "starlink-durable-e2e",
        ),
        clock=clock,
    ).execute(plan, cast(RadioDevice, radio), SigMFRecordingWriter(), spool)
    if completed.data_object.byte_count != SEGMENTS_PER_RECORDING * SAMPLE_COUNT * 8:
        raise RuntimeError("capture emitted an unexpected paired-IQ byte count")
    local = RootedSigMFRecordingStore(capture_root / "recordings")
    receipt = PublicationReconciler(
        spool,
        RecordingPublisherAdapter(local, blobs, recording_catalog),
        local,
    ).reconcile()
    if (receipt.published, receipt.cleaned, receipt.deferred) != (1, 1, 0):
        raise RuntimeError("durable recording handoff did not complete atomically")
    published = recording_catalog.get(str(recording_id))
    if published is None:
        raise RuntimeError("durable recording catalog omitted the published pair")
    if spool.get(recording_id).state is not SpoolState.CLEANED:
        raise RuntimeError("local spool did not reach cleaned after durable handoff")

    truth_ref = blobs.put(
        io.BytesIO(fixture.truth_json),
        expected_digest=Digest.sha256(fixture.truth_json),
        expected_bytes=len(fixture.truth_json),
        media_type="application/json",
        format_id="paired-starlink-scan-truth-v1",
        idempotency_key=f"truth:{case.case_id}",
    )
    request = RecordingAnalysisRequest(
        SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording_id,
        published.recording_object,
        detector_suite_algorithm_ref(),
        detector_suite_config_ref(CONFIG),
        (),
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    restarted_reader = FileSystemBlobReader(workspace / "cas")
    with SigMFRecordingObjectReader(restarted_reader).open(
        published.recording_object
    ) as view:
        bundle = IndependentDetectorSuite(
            CONFIG,
            AnalysisExecutionContext(
                "starlink-durable-e2e",
                "1",
                "offline-harness",
                Digest.sha256(b"starlink-durable-e2e-environment-v1"),
                UtcNs(100),
                UtcNs(101),
                "offline-harness-host",
            ),
        ).analyze(view, request)
    feature_ref = features.publish(
        request, bundle, idempotency_key=f"feature:{case.case_id}"
    )
    with features.open(feature_ref) as opened:
        if opened.bundle() != bundle:
            raise RuntimeError("durable FeatureSet round trip changed content")
    return _Analyzed(
        case,
        published,
        fixture.truth,
        truth_ref,
        bundle,
        feature_ref,
        int(completed.manifest.capture_started_utc_ns),
    )


def _failure_receipt(workspace: Path, kind: str) -> dict[str, Any]:
    case = _Case(f"failure_{kind}", f"failure_{kind}", DatasetSplit.TRAIN, True, 909, 1)
    index = 90 if kind == "truncation" else 91
    plan = _plan(index, suffix=f"_{kind}")
    fixture = _fixture(case, plan)
    scripts = _radio_scripts(fixture, plan, index)
    first = plan.activities[0].segments[0]
    payload = fixture.payload_for(first.segment_id)
    if kind == "truncation":
        scripts[first.segment_id] = (
            V5Refill(
                payload[:-8],
                _metadata(index, 0, count=SAMPLE_COUNT - 1),
            ),
        )
        expected_error: type[Exception] = SampleCountError
    elif kind == "missing_frame":
        half_bytes = SAMPLE_COUNT // 2 * 8
        scripts[first.segment_id] = (
            V5Refill(
                payload[:half_bytes],
                _metadata(index, 0, count=SAMPLE_COUNT // 2),
            ),
            V5Refill(
                payload[half_bytes:],
                _metadata(
                    index,
                    0,
                    refill_index=1,
                    offset=SAMPLE_COUNT // 2,
                    count=SAMPLE_COUNT // 2,
                    first_sequence_delta=1,
                ),
            ),
        )
        expected_error = ContinuityError
    else:
        raise ValueError("unsupported failure injection")
    clock = _Clock(1_750_000_100_000_000_000 + index)
    recording_id = RecordingId(f"rec_durable_e2e_failure_{kind}")
    capture_root = workspace / "failures" / kind
    spool = SQLiteLocalSpool(
        capture_root / "capture.sqlite3",
        capture_root / "recordings",
        id_factory=lambda: recording_id,
        now_ns=clock.now_utc_ns,
    )
    radio = FakeV5PairedRadio(
        RADIO,
        RECEIVERS,
        scripts,
        CaptureProvenance("v5", "offline-harness", "0.25", "v3", "metadata=1"),
        continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        clock=clock,
    )
    try:
        PlanCaptureEngine(
            CaptureIdentity(
                StationId("station_offline_durable"),
                "synthetic-no-hardware",
                "deterministic-clock",
                HardwareSnapshotId("hw_offline_durable"),
                "starlink-durable-e2e-failure-injection",
            ),
            clock=clock,
        ).execute(plan, cast(RadioDevice, radio), SigMFRecordingWriter(), spool)
    except expected_error as error:
        entry = spool.get(recording_id)
        if entry.state is not SpoolState.FAILED or entry.recording is not None:
            raise RuntimeError(
                "failed capture entered a durable success state"
            ) from error
        partials = tuple(capture_root.rglob("*.partial"))
        finalized = tuple((capture_root / "recordings").glob("rec_*"))
        if partials or finalized:
            raise RuntimeError("failed capture retained recording artifacts") from error
        return {
            "kind": kind,
            "outcome": "rejected",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "spool_state": entry.state.value,
            "durable_recording_published": False,
            "partial_artifact_retained": False,
        }
    raise RuntimeError(f"{kind} failure injection unexpectedly succeeded")


def _verify_backgrounds(analyzed: Sequence[_Analyzed]) -> None:
    nulls = {
        item.case.group_id: item for item in analyzed if not item.case.signal_present
    }
    for item in analyzed:
        base = nulls[item.case.group_id]
        base_segments = {
            (segment["channel"], segment["edge"]): segment
            for segment in base.truth["segments"]
        }
        for segment in item.truth["segments"]:
            background = base_segments[(segment["channel"], segment["edge"])]
            for receiver, base_receiver in zip(
                segment["receivers"], background["receivers"], strict=True
            ):
                if (
                    receiver["base_noise_ci16_sha256"]
                    != base_receiver["base_noise_ci16_sha256"]
                ):
                    raise RuntimeError("counterfactual background lineage changed")


def _candidates(analyzed: Sequence[_Analyzed]) -> tuple[DatasetCandidate, ...]:
    _verify_backgrounds(analyzed)
    nulls = {
        item.case.group_id: item for item in analyzed if not item.case.signal_present
    }
    independent = (SUITE_METHOD_ID,) + METHOD_IDS
    return tuple(
        DatasetCandidate(
            feature_set_id=str(item.feature_ref.feature_set_id),
            feature_set_digest=item.feature_ref.bundle_ref.digest,
            recording_id=str(item.published.recording_object.recording_id),
            split_group_id=item.case.group_id,
            captured_utc_ns=item.captured_utc_ns,
            radio_id=str(RADIO),
            lnb_ids=("synthetic-if-no-lnb",),
            observation_mode="synthetic-durable-e2e-4096",
            sample_rate_hz=SAMPLE_RATE_HZ,
            gain_mode="agc",
            gain_db=None,
            satellite_id=None,
            truth=TruthLabel(
                item.case.signal_present,
                LabelSource.INJECTED,
                (
                    LabelEvidence(
                        LabelSource.INJECTED,
                        item.truth_ref.digest,
                        "starlink-durable-e2e-v1",
                        item.captured_utc_ns,
                        independent,
                        uncertainty=(("scope", "coded-edge-pilot-approximation"),),
                        base_recording_digest=nulls[
                            item.case.group_id
                        ].published.recording_object.identity_digest(),
                        injection_spec_digest=item.truth_ref.digest,
                    ),
                ),
                confidence=1.0,
            ),
            derived_from_recording_id=(
                str(nulls[item.case.group_id].published.recording_object.recording_id)
                if item.case.signal_present
                else None
            ),
        )
        for item in analyzed
    )


def _unique_artifact_bytes(refs: Sequence[ObjectRef]) -> int:
    unique = {(str(ref.digest), ref.byte_count) for ref in refs}
    return sum(byte_count for _, byte_count in unique)


def run_harness(workspace: Path) -> HarnessResult:
    """Run the complete deterministic offline harness in a fresh local workspace."""

    started = time.perf_counter()
    workspace = Path(workspace)
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("harness workspace must be absent or empty")
    workspace.mkdir(parents=True, exist_ok=True)
    cases = _cases()
    if len(cases) > MAX_CASES:
        raise RuntimeError("case count exceeds frozen harness bound")
    blobs = FileSystemBlobStore(workspace / "cas")
    recording_catalog = InMemoryRecordingCatalog()
    feature_catalog = _FeatureCatalog()
    features = DurableFeatureSetRepository(blobs, feature_catalog)
    analyzed = tuple(
        _capture_analyze(
            workspace,
            case,
            index,
            blobs,
            recording_catalog,
            features,
        )
        for index, case in enumerate(cases)
    )
    detector_windows = sum(len(item.bundle.method_scores) for item in analyzed) // len(
        METHOD_IDS
    )
    if detector_windows > MAX_DETECTOR_WINDOWS:
        raise RuntimeError("detector windows exceed frozen harness bound")

    candidates = _candidates(analyzed)
    assignments = {item.case.group_id: item.case.split for item in analyzed}
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
        selection_spec="starlink-durable-e2e-v1:explicit-three-group-split",
        selection_cutoff_utc_ns=UtcNs(max(item.captured_utc_ns for item in analyzed)),
    )
    dataset_catalog = _DatasetCatalog()
    datasets = DurableDatasetSnapshotRepository(blobs, dataset_catalog)
    dataset_ref = datasets.publish(dataset, idempotency_key="dataset:durable-e2e")
    if datasets.get(dataset_ref) != dataset:
        raise RuntimeError("durable dataset round trip changed content")

    train = tuple(
        FrozenTrainCalibrationMember(
            item.bundle, item.case.signal_present, item.case.group_id
        )
        for item in analyzed
        if item.case.split is DatasetSplit.TRAIN
    )
    threshold_rule = calibrate_train_thresholds(train, expected_method_ids=METHOD_IDS)
    evaluation = evaluate_detectors(
        dataset,
        {str(item.bundle.feature_set_id): item.bundle for item in analyzed},
        threshold_rule,
    )
    evaluation_catalog = _EvaluationCatalog()
    evaluations = DurableDetectorEvaluationRepository(blobs, evaluation_catalog)
    evaluation_ref = evaluations.publish(
        EvaluationRunId("erun_starlink_durable_e2e_v1"),
        evaluation,
        idempotency_key="evaluation:durable-e2e",
    )
    with evaluations.open(evaluation_ref) as opened:
        if opened.report != evaluation:
            raise RuntimeError("durable evaluation round trip changed content")

    failures = (
        _failure_receipt(workspace, "truncation"),
        _failure_receipt(workspace, "missing_frame"),
    )
    dataset_object = dataset_catalog.entries[dataset_ref].bundle_ref
    artifact_objects = tuple(
        ref
        for item in analyzed
        for ref in (
            item.published.recording_object.data_object,
            item.published.recording_object.metadata_object,
            item.truth_ref,
            item.feature_ref.bundle_ref,
        )
    ) + (dataset_object, evaluation_ref.report_object)
    durable_bytes = _unique_artifact_bytes(artifact_objects)
    generated_iq_bytes = len(analyzed) * SEGMENTS_PER_RECORDING * SAMPLE_COUNT * 8
    if generated_iq_bytes > MAX_GENERATED_IQ_BYTES:
        raise RuntimeError("generated IQ exceeds frozen harness bound")
    if durable_bytes > MAX_DURABLE_ARTIFACT_BYTES:
        raise RuntimeError("durable artifacts exceed frozen harness bound")

    result: dict[str, Any] = {
        "pipeline": (
            "capture-compatible synthetic paired CI16 -> RecordingManifest -> "
            "SQLite spool/filesystem recording -> filesystem CAS handoff -> "
            "IndependentDetectorSuite -> frozen dataset -> detector evaluation"
        ),
        "offline_only": True,
        "durable_handoff_verified_after_reader_reconstruction": True,
        "split_policy": {
            "assignment": "explicit-whole-background-group",
            "threshold_calibration_split": "train",
            "groups": {
                split.value: sorted(
                    {
                        item.case.group_id
                        for item in analyzed
                        if item.case.split is split
                    }
                )
                for split in DatasetSplit
            },
        },
        "bounds": {
            "max_cases": MAX_CASES,
            "max_detector_windows": MAX_DETECTOR_WINDOWS,
            "max_generated_iq_bytes": MAX_GENERATED_IQ_BYTES,
            "max_durable_artifact_bytes": MAX_DURABLE_ARTIFACT_BYTES,
            "max_report_bytes": MAX_REPORT_BYTES,
            "max_runtime_seconds": MAX_RUNTIME_SECONDS,
        },
        "observed": {
            "case_count": len(analyzed),
            "segment_count": len(analyzed) * SEGMENTS_PER_RECORDING,
            "detector_window_count": detector_windows,
            "generated_iq_bytes": generated_iq_bytes,
            "durable_artifact_bytes": durable_bytes,
        },
        "artifact_identities": {
            "recordings": [item.published.recording_object for item in analyzed],
            "truth_objects": [item.truth_ref for item in analyzed],
            "feature_sets": [item.feature_ref for item in analyzed],
            "dataset_snapshot": dataset_ref,
            "dataset_object": dataset_object,
            "threshold_rule_id": threshold_rule.rule_id,
            "threshold_rule_digest": threshold_rule.digest,
            "evaluation": evaluation_ref,
        },
        "handoff_receipts": [
            {
                "case_id": item.case.case_id,
                "split": item.case.split.value,
                "recording_id": item.published.recording_object.recording_id,
                "manifest_digest": item.published.recording_object.manifest_digest,
                "data_object": item.published.recording_object.data_object,
                "metadata_object": item.published.recording_object.metadata_object,
                "spool_state": "cleaned",
                "cas_verified_after_reader_reconstruction": True,
            }
            for item in analyzed
        ],
        "failure_injections": failures,
        "detector_evaluation": json.loads(evaluation.canonical_bytes()),
        "scientific_limitations": (
            "Synthetic uniform noise and a coded edge-pilot approximation do not represent real receiver noise or a complete Starlink waveform.",
            "Each recording contains one 4096-sample window per segment; this is interface and failure-behavior evidence, not a production-duration sensitivity estimate.",
            "The deterministic locked-test labels are held out from fitting but are neither sealed nor blinded.",
            "Filesystem durability is exercised locally; PostgreSQL, shared storage, live capture, RF safety, and cross-host behavior are outside this harness.",
        ),
    }
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "result_digest": Digest.sha256(canonical_json_bytes(result)),
        "result": result,
    }
    report_bytes = canonical_json_bytes(report)
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise RuntimeError("machine-readable report exceeds frozen harness bound")
    report_object = blobs.put(
        io.BytesIO(report_bytes),
        expected_digest=Digest.sha256(report_bytes),
        expected_bytes=len(report_bytes),
        media_type="application/json",
        format_id="starlink-durable-e2e-report-v1",
        idempotency_key="report:durable-e2e",
    )
    with FileSystemBlobReader(workspace / "cas").open(report_object) as stream:
        if stream.read() != report_bytes:
            raise RuntimeError("durable report round trip changed content")
    elapsed = time.perf_counter() - started
    if elapsed > MAX_RUNTIME_SECONDS:
        raise RuntimeError("harness runtime exceeds frozen bound")
    return HarnessResult(report, report_bytes, report_object, elapsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_harness(args.workspace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result.report_bytes)
    print(
        f"wrote {args.output} ({len(result.report_bytes)} bytes, "
        f"{result.report_object.digest}, {result.elapsed_seconds:.3f}s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
