"""Bounded offline detector benchmark over frozen Starlink pilot backgrounds.

The command composes the detector-independent paired scan fixture, the normal
recording detector suite, TRAIN-only threshold calibration, frozen dataset
contracts, and the standard detector evaluator.  It writes exactly one
aggregate JSON report and never contacts a radio, database, or network service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from benchmark.starlink_e2e_calibration import (
    FrozenTrainCalibrationMember,
    calibrate_train_thresholds,
)
from benchmark.starlink_pilot_if import EDGE_PILOT_SUBCARRIERS
from benchmark.starlink_scan_fixture import (
    PairedStarlinkScanFixture,
    ReceiverPath,
    StarlinkPilotScanCase,
    generate_paired_starlink_scan_fixture,
)
from leo_flow.analysis.dataset import (
    DatasetCandidate,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    carve_dataset,
    evaluate_detectors,
    freeze_dataset_snapshot,
)
from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DetectorSuiteConfig,
    IndependentDetectorSuite,
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
    encode_feature_set,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import (
    ActivityManifest,
    CapturePlan,
    GainMode,
    GainSetting,
    RecordingManifest,
    SegmentManifest,
)
from leo_flow.contracts.continuity import ContiguousRfSpan, SafeSampleWindow
from leo_flow.contracts.core import (
    Digest,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.features import (
    FeatureSetBundle,
    FeatureSetRef,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import RecordingView

SCHEMA = "leo-flow.starlink-detector-matrix-report/v1"
SPEC_SCHEMA = "leo-flow.starlink-detector-matrix-spec/v1"
DEFAULT_SPEC = Path(__file__).with_name("specs") / "starlink-detector-matrix-v1.json"
METHOD_IDS = (
    "coarse-energy@0.1.0",
    "paired-common-mode@0.1.0",
    "periodic-coherence@0.1.0",
)
SUITE_METHOD_ID = "independent-detector-suite@0.1.0"
PER_SNR_INTERPRETATION = (
    "Composite-arm target detection rates. Each nominal source SNR is bound to "
    "its declared pilot subset, CFO, edge flip, target-channel offset, and "
    "near-clipping setting; these results are not an isolated SNR response curve."
)
RADIO = RadioId("radio_synthetic_starlink_matrix")
RECEIVERS = (
    ReceiverChainId("rx_synthetic_matrix_1"),
    ReceiverChainId("rx_synthetic_matrix_2"),
)


class MatrixSpecificationError(ValueError):
    """The frozen benchmark matrix does not satisfy its declared invariants."""


@dataclass(frozen=True)
class MatrixCondition:
    condition_id: str
    snr_db: float | None
    pilot_subset: Literal["inner", "full"]
    edge_flip: bool
    cfo_hz: float
    target_channel_offset: int
    near_clipping: bool

    @property
    def signal_present(self) -> bool:
        return self.snr_db is not None


@dataclass(frozen=True)
class BackgroundGroup:
    group_id: str
    split: DatasetSplit
    seed_u64: int
    base_edge: Literal["lower", "upper"]
    target_channel_base: int
    second_receiver: ReceiverPath


@dataclass(frozen=True)
class MatrixSpec:
    raw: Mapping[str, Any]
    sample_rate_hz: int
    bandwidth_hz: int
    sample_count: int
    ambient_noise_rms_counts: float
    converter_min: int
    converter_max: int
    detector_config: DetectorSuiteConfig
    conditions: tuple[MatrixCondition, ...]
    groups: tuple[BackgroundGroup, ...]

    @property
    def digest(self) -> Digest:
        return Digest.sha256(_canonical_json(self.raw))


@dataclass(frozen=True)
class ExpandedCase:
    case_id: str
    group: BackgroundGroup
    condition: MatrixCondition
    edge: Literal["lower", "upper"]
    pilot_indices: tuple[int, ...]
    target_channel: int


@dataclass(frozen=True)
class AnalyzedCase:
    case: ExpandedCase
    bundle: FeatureSetBundle
    feature_ref: FeatureSetRef
    recording_ref: RecordingObjectRef
    truth: Mapping[str, Any]
    truth_digest: Digest
    captured_utc_ns: int
    paired_iq_bytes: int


class _MemoryRecordingView:
    """Minimal immutable RecordingView adapter for generated paired CI16."""

    def __init__(
        self,
        manifest: RecordingManifest,
        payloads: Mapping[SegmentId, bytes],
    ) -> None:
        self._manifest = manifest
        self._payloads = dict(payloads)

    @property
    def manifest(self) -> RecordingManifest:
        return self._manifest

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes:
        payload = self._payloads[segment_id]
        if not 0 <= start_sample < stop_sample <= len(payload) // 8:
            raise ValueError("matrix reader received an out-of-bounds request")
        return payload[start_sample * 8 : stop_sample * 8]

    def continuity(self, segment_id: SegmentId) -> None:
        if segment_id not in self._payloads:
            raise KeyError(segment_id)

    def contiguous_rf_spans(
        self, segment_id: SegmentId
    ) -> tuple[ContiguousRfSpan, ...]:
        count = len(self._payloads[segment_id]) // 8
        return (ContiguousRfSpan(0, count, 0, count),)

    def iter_safe_windows(
        self, segment_id: SegmentId, window_samples: int, stride_samples: int
    ) -> Iterator[SafeSampleWindow]:
        count = len(self._payloads[segment_id]) // 8
        if count < window_samples:
            return
        starts = list(range(0, count - window_samples + 1, stride_samples))
        last = count - window_samples
        if starts[-1] != last:
            starts.append(last)
        yield from (SafeSampleWindow(start, start + window_samples) for start in starts)


def load_matrix_spec(path: Path = DEFAULT_SPEC) -> MatrixSpec:
    """Load and validate the canonical matrix without generating IQ."""

    try:
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise MatrixSpecificationError(f"cannot load matrix spec: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != SPEC_SCHEMA:
        raise MatrixSpecificationError("unsupported detector matrix spec schema")
    acquisition = _mapping(raw, "acquisition")
    detector = _mapping(raw, "detector_config")
    sample_rate_hz = _positive_int(acquisition, "sample_rate_hz")
    bandwidth_hz = _positive_int(acquisition, "bandwidth_hz")
    sample_count = _positive_int(acquisition, "sample_count")
    if bandwidth_hz > sample_rate_hz:
        raise MatrixSpecificationError("bandwidth cannot exceed sample rate")
    ambient_noise = _finite_positive(acquisition, "ambient_noise_rms_counts")
    converter_min = _integer(acquisition, "converter_min")
    converter_max = _integer(acquisition, "converter_max")
    if not -32768 <= converter_min < converter_max <= 32767:
        raise MatrixSpecificationError("converter envelope must fit CI16")
    config = DetectorSuiteConfig(
        window_samples=_positive_int(detector, "window_samples"),
        stride_samples=_positive_int(detector, "stride_samples"),
        periodic_lag_samples=_positive_int(detector, "periodic_lag_samples"),
        max_pair_delay_samples=_nonnegative_int(detector, "max_pair_delay_samples"),
        clip_threshold_abs=_positive_int(detector, "clip_threshold_abs"),
        refuse_clipping=True,
    )
    if config.window_samples != sample_count or config.stride_samples != sample_count:
        raise MatrixSpecificationError(
            "canonical matrix requires exactly one aligned detector window per segment"
        )
    conditions = _load_conditions(raw.get("conditions"))
    groups = _load_groups(raw.get("background_groups"), config)
    _validate_coverage(conditions, groups)
    return MatrixSpec(
        raw=raw,
        sample_rate_hz=sample_rate_hz,
        bandwidth_hz=bandwidth_hz,
        sample_count=sample_count,
        ambient_noise_rms_counts=ambient_noise,
        converter_min=converter_min,
        converter_max=converter_max,
        detector_config=config,
        conditions=conditions,
        groups=groups,
    )


def expand_cases(spec: MatrixSpec) -> tuple[ExpandedCase, ...]:
    """Resolve every matrix dimension before fixture generation or analysis."""

    output: list[ExpandedCase] = []
    for group in spec.groups:
        for condition in spec.conditions:
            edge: Literal["lower", "upper"] = group.base_edge
            if condition.edge_flip:
                edge = "upper" if edge == "lower" else "lower"
            available = EDGE_PILOT_SUBCARRIERS[edge]
            pilots = available[3:5] if condition.pilot_subset == "inner" else available
            channel = (
                (group.target_channel_base - 1 + condition.target_channel_offset) % 4
            ) + 1
            output.append(
                ExpandedCase(
                    case_id=f"{group.group_id}_{condition.condition_id}",
                    group=group,
                    condition=condition,
                    edge=edge,
                    pilot_indices=tuple(pilots),
                    target_channel=channel,
                )
            )
    return tuple(output)


def run_benchmark(spec: MatrixSpec) -> dict[str, Any]:
    """Execute the offline matrix and return its one aggregate report."""

    total_started = time.perf_counter()
    generation_seconds = 0.0
    analysis_seconds = 0.0
    analyzed: list[AnalyzedCase] = []
    cases = expand_cases(spec)
    analyzer = IndependentDetectorSuite(
        spec.detector_config,
        AnalysisExecutionContext(
            producer_name="starlink-detector-matrix",
            producer_version="1",
            git_commit="offline-benchmark",
            environment_digest=canonical_digest(
                {
                    "python": platform.python_version(),
                    "implementation": platform.python_implementation(),
                }
            ),
            started_utc_ns=UtcNs(1_900_000_000_000_000_000),
            completed_utc_ns=UtcNs(1_900_000_000_000_000_001),
            host_class="offline-benchmark-host",
        ),
    )
    for case_index, case in enumerate(cases):
        plan = _plan(spec, case, case_index)
        before = time.perf_counter()
        fixture = _fixture(spec, case, plan)
        generation_seconds += time.perf_counter() - before
        view, recording_ref, captured = _recording_view(
            spec, case, plan, fixture, case_index
        )
        request = RecordingAnalysisRequest(
            schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
            recording_id=recording_ref.recording_id,
            recording_object_ref=recording_ref,
            algorithm_ref=detector_suite_algorithm_ref(),
            config_ref=detector_suite_config_ref(spec.detector_config),
            dependency_refs=(),
            requested_output_schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
        before = time.perf_counter()
        bundle = analyzer.analyze(cast(RecordingView, view), request)
        analysis_seconds += time.perf_counter() - before
        observed_methods = {
            f"{score.method_id}@{score.method_version}"
            for score in bundle.method_scores
        }
        if observed_methods != set(METHOD_IDS):
            raise RuntimeError(
                f"case {case.case_id} method membership differs: {sorted(observed_methods)}"
            )
        expected_scores = len(fixture.segments) * len(METHOD_IDS)
        if len(bundle.method_scores) != expected_scores:
            raise RuntimeError(
                f"case {case.case_id} emitted {len(bundle.method_scores)} scores; "
                f"expected {expected_scores}"
            )
        bundle_bytes = encode_feature_set(bundle)
        bundle_object = ObjectRef(
            Digest.sha256(bundle_bytes),
            len(bundle_bytes),
            "application/json",
            "feature-set-bundle-v0.1",
            f"memory:feature:{case.case_id}",
        )
        analyzed.append(
            AnalyzedCase(
                case=case,
                bundle=bundle,
                feature_ref=FeatureSetRef(
                    bundle.feature_set_id, bundle.analysis_run_id, bundle_object
                ),
                recording_ref=recording_ref,
                truth=fixture.truth,
                truth_digest=Digest.sha256(fixture.truth_json),
                captured_utc_ns=captured,
                paired_iq_bytes=sum(
                    len(item.paired_ci16_le) for item in fixture.segments
                ),
            )
        )

    aggregation_started = time.perf_counter()
    frozen = tuple(analyzed)
    _verify_background_lineage(frozen)
    candidates = _candidates(frozen, spec)
    assignments = {group.group_id: group.split for group in spec.groups}
    carved = carve_dataset(
        candidates,
        group_partitions=assignments,
        evaluated_method_id=SUITE_METHOD_ID,
        require_promotion=True,
    )
    dataset = freeze_dataset_snapshot(
        carved,
        candidates,
        (item.feature_ref for item in frozen),
        selection_spec=f"starlink-detector-matrix-v1:{spec.digest.value}",
        selection_cutoff_utc_ns=UtcNs(max(item.captured_utc_ns for item in frozen)),
    )
    training = tuple(
        FrozenTrainCalibrationMember(
            item.bundle,
            item.case.condition.signal_present,
            item.case.group.group_id,
        )
        for item in frozen
        if item.case.group.split is DatasetSplit.TRAIN
    )
    rule = calibrate_train_thresholds(training, expected_method_ids=METHOD_IDS)
    evaluation = evaluate_detectors(
        dataset,
        {str(item.bundle.feature_set_id): item.bundle for item in frozen},
        rule,
    )
    summaries = _aggregate_summaries(
        frozen,
        rule.thresholds,
        converter_min=spec.converter_min,
        converter_max=spec.converter_max,
    )
    aggregation_seconds = time.perf_counter() - aggregation_started
    total_seconds = time.perf_counter() - total_started
    feature_bytes = sum(item.feature_ref.bundle_ref.byte_count for item in frozen)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "benchmark_identity": {
            "matrix_spec_sha256": spec.digest.value,
            "dataset_snapshot_id": str(dataset.feature_dataset.snapshot_id),
            "dataset_snapshot_digest": str(dataset.snapshot_digest),
            "feature_membership_digest": str(dataset.feature_dataset.membership_digest),
            "threshold_rule_id": rule.rule_id,
            "threshold_rule_digest": str(rule.digest),
            "algorithm_digest": str(detector_suite_algorithm_ref().digest),
            "detector_config_digest": str(
                detector_suite_config_ref(spec.detector_config).digest
            ),
        },
        "matrix_coverage": _coverage_report(spec, cases),
        "thresholds": {method: value for method, value in rule.thresholds},
        "recording_level_confusion": _recording_confusion(evaluation),
        "segment_level_confusion": summaries["segment_level_confusion"],
        "per_snr_detection": {
            "interpretation": PER_SNR_INTERPRETATION,
            "condition_arms_by_nominal_source_snr_db": _condition_arms(spec),
            "methods": summaries["per_snr_detection"],
        },
        "achieved_rx_snr_db": summaries["achieved_rx_snr_db"],
        "converter_margin": summaries["converter_margin"],
        "association": {
            "overall": _association_json(evaluation.overall_association),
            "by_split": {
                item.split: _association_json(item.association)
                for item in evaluation.association_by_split
            },
        },
        "standard_detector_evaluation": json.loads(evaluation.canonical_bytes()),
        "runtime": {
            "fixture_generation_seconds": round(generation_seconds, 6),
            "detector_analysis_seconds": round(analysis_seconds, 6),
            "aggregation_seconds": round(aggregation_seconds, 6),
            "total_seconds": round(total_seconds, 6),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "size": {
            "recording_count": len(frozen),
            "segment_count": len(frozen) * 8,
            "detector_window_count": len(frozen) * 8,
            "generated_paired_iq_bytes": sum(item.paired_iq_bytes for item in frozen),
            "encoded_feature_set_bytes": feature_bytes,
            "output_json_bytes": 0,
        },
        "scientific_limitations": (
            "The background is deterministic independent uniform synthetic noise, not recorded receiver noise, colored interference, or an RF environment.",
            "The injected signal contains published coded edge pilots only; it is not a complete Starlink downlink waveform.",
            "Receiver impairments are limited to scalar gain and phase plus causal integer delay; there is no multipath, clock offset, drift, or time-varying channel.",
            "Achieved RX SNR is a pre-quantization fixture measurement over active delayed pilot samples, not a calibrated hardware measurement.",
            "Each segment contributes one 4096-sample window; segment results must not be interpreted as independent long-duration observations.",
            PER_SNR_INTERPRETATION,
            "The locked_test partition is held out from threshold fitting but its deterministic synthetic labels are not sealed or blinded.",
            "Coarse energy and common-mode evidence are not Starlink-specific and may respond to unrelated narrowband or correlated signals.",
            "This offline benchmark does not qualify radio safety, capture continuity, shared storage, PostgreSQL, or cross-host operation.",
        ),
    }
    _stabilize_output_size(report)
    return report


def _load_conditions(value: Any) -> tuple[MatrixCondition, ...]:
    if not isinstance(value, list) or not value:
        raise MatrixSpecificationError("conditions must be a non-empty array")
    output: list[MatrixCondition] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise MatrixSpecificationError(f"condition {index} must be an object")
        condition_id = _token(raw, "condition_id")
        snr = raw.get("snr_db")
        if snr is not None and (
            isinstance(snr, bool)
            or not isinstance(snr, (int, float))
            or not math.isfinite(snr)
        ):
            raise MatrixSpecificationError(
                f"{condition_id}: snr_db must be finite or null"
            )
        subset = raw.get("pilot_subset")
        if subset not in ("inner", "full"):
            raise MatrixSpecificationError(f"{condition_id}: invalid pilot subset")
        edge_flip = raw.get("edge_flip")
        near = raw.get("near_clipping")
        if not isinstance(edge_flip, bool) or not isinstance(near, bool):
            raise MatrixSpecificationError(
                f"{condition_id}: edge_flip and near_clipping must be booleans"
            )
        output.append(
            MatrixCondition(
                condition_id,
                None if snr is None else float(snr),
                subset,
                edge_flip,
                _finite(raw, "cfo_hz"),
                _integer(raw, "target_channel_offset"),
                near,
            )
        )
    if len({item.condition_id for item in output}) != len(output):
        raise MatrixSpecificationError("condition IDs must be unique")
    return tuple(output)


def _load_groups(
    value: Any, config: DetectorSuiteConfig
) -> tuple[BackgroundGroup, ...]:
    if not isinstance(value, list) or not value:
        raise MatrixSpecificationError("background_groups must be a non-empty array")
    output: list[BackgroundGroup] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise MatrixSpecificationError(
                f"background group {index} must be an object"
            )
        group_id = _token(raw, "group_id")
        try:
            split = DatasetSplit(raw.get("split"))
        except ValueError as error:
            raise MatrixSpecificationError(
                f"{group_id}: invalid explicit split"
            ) from error
        base_edge = raw.get("base_edge")
        if base_edge not in ("lower", "upper"):
            raise MatrixSpecificationError(f"{group_id}: invalid base edge")
        path = _mapping(raw, "second_receiver")
        receiver = ReceiverPath(
            integer_delay_samples=_nonnegative_int(path, "integer_delay_samples"),
            gain_linear=_finite_positive(path, "gain_linear"),
            phase_offset_rad=_finite(path, "phase_offset_rad"),
            ambient_noise_rms_counts=0.0,
        )
        if receiver.integer_delay_samples > config.max_pair_delay_samples:
            raise MatrixSpecificationError(
                f"{group_id}: delay exceeds detector search bound"
            )
        output.append(
            BackgroundGroup(
                group_id,
                split,
                _positive_int(raw, "seed_u64"),
                base_edge,
                _positive_int(raw, "target_channel_base"),
                receiver,
            )
        )
    if len({item.group_id for item in output}) != len(output):
        raise MatrixSpecificationError("background group IDs must be unique")
    return tuple(output)


def _validate_coverage(
    conditions: tuple[MatrixCondition, ...], groups: tuple[BackgroundGroup, ...]
) -> None:
    if len(groups) < 20:
        raise MatrixSpecificationError(
            "canonical matrix requires at least 20 backgrounds"
        )
    if {group.split for group in groups} != set(DatasetSplit):
        raise MatrixSpecificationError("all three explicit dataset splits are required")
    if not any(not condition.signal_present for condition in conditions):
        raise MatrixSpecificationError("matrix requires a null condition")
    snrs = {condition.snr_db for condition in conditions if condition.signal_present}
    if len(snrs) < 3:
        raise MatrixSpecificationError(
            "matrix requires at least three signal SNR levels"
        )
    if {condition.pilot_subset for condition in conditions} != {"inner", "full"}:
        raise MatrixSpecificationError("matrix requires inner and full pilot subsets")
    if not any(condition.near_clipping for condition in conditions):
        raise MatrixSpecificationError("matrix requires a near-clipping condition")
    if len({condition.cfo_hz for condition in conditions}) < 3:
        raise MatrixSpecificationError("matrix requires multiple CFO conditions")
    if {group.base_edge for group in groups} != {"lower", "upper"}:
        raise MatrixSpecificationError("matrix requires both pilot edges")
    if len({group.seed_u64 for group in groups}) != len(groups):
        raise MatrixSpecificationError("each background group needs a unique seed")
    if any(not 1 <= group.target_channel_base <= 4 for group in groups):
        raise MatrixSpecificationError("target channel bases must lie in [1, 4]")
    if len({group.second_receiver.gain_linear for group in groups}) < 3:
        raise MatrixSpecificationError("matrix requires receiver gain variation")
    if len({group.second_receiver.phase_offset_rad for group in groups}) < 3:
        raise MatrixSpecificationError("matrix requires receiver phase variation")
    if len({group.second_receiver.integer_delay_samples for group in groups}) < 3:
        raise MatrixSpecificationError("matrix requires receiver delay variation")


def _plan(spec: MatrixSpec, case: ExpandedCase, index: int) -> CapturePlan:
    return build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=PlanId(f"plan_matrix_{index:03d}"),
            radio_id=RADIO,
            receiver_chain_ids=RECEIVERS,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=float(spec.sample_rate_hz),
            bandwidth_hz=float(spec.bandwidth_hz),
            sample_count=spec.sample_count,
            edge_order="L" if index % 2 == 0 else "U",
            edge_order_draw_u32=index % 2,
            arm_name="offline-detector-matrix-4096",
            hardware_block_samples=spec.sample_count,
        )
    )


def _fixture(
    spec: MatrixSpec, case: ExpandedCase, plan: CapturePlan
) -> PairedStarlinkScanFixture:
    signal_rms = (
        spec.ambient_noise_rms_counts * 1e-6
        if case.condition.snr_db is None
        else spec.ambient_noise_rms_counts * 10 ** (case.condition.snr_db / 20)
    )
    second = case.group.second_receiver
    return generate_paired_starlink_scan_fixture(
        plan,
        StarlinkPilotScanCase(
            signal_present=case.condition.signal_present,
            target_channels=(case.target_channel,),
            edge=case.edge,
            pilot_indices=case.pilot_indices,
            seed_u64=case.group.seed_u64,
            receiver_paths=(
                ReceiverPath(ambient_noise_rms_counts=spec.ambient_noise_rms_counts),
                ReceiverPath(
                    integer_delay_samples=second.integer_delay_samples,
                    gain_linear=second.gain_linear,
                    phase_offset_rad=second.phase_offset_rad,
                    ambient_noise_rms_counts=spec.ambient_noise_rms_counts,
                ),
            ),
            source_signal_rms_counts=signal_rms,
            cfo_hz=case.condition.cfo_hz,
            converter_min=spec.converter_min,
            converter_max=spec.converter_max,
        ),
    )


def _recording_view(
    spec: MatrixSpec,
    case: ExpandedCase,
    plan: CapturePlan,
    fixture: PairedStarlinkScanFixture,
    case_index: int,
) -> tuple[_MemoryRecordingView, RecordingObjectRef, int]:
    requests = tuple(
        segment for activity in plan.activities for segment in activity.segments
    )
    payloads = {item.segment_id: item.paired_ci16_le for item in fixture.segments}
    capture_start = 1_800_000_000_000_000_000 + case_index * 1_000_000_000
    manifests = tuple(
        SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=request.center_frequency_hz,
            actual_sample_rate_hz=request.sample_rate_hz,
            actual_bandwidth_hz=request.bandwidth_hz,
            actual_gain=request.gain,
            start_utc_ns=UtcNs(capture_start + index * 10_000_000),
            monotonic_start_ns=index * 10_000_000,
            sample_count=spec.sample_count,
            shape=(spec.sample_count, 2, 2),
        )
        for index, request in enumerate(requests)
    )
    finish = int(manifests[-1].start_utc_ns) + max(
        1, round(spec.sample_count * 1e9 / spec.sample_rate_hz)
    )
    activity_request = plan.activities[0]
    manifest = RecordingManifest(
        schema=SchemaRef(RecordingManifest.SCHEMA_ID),
        recording_id=RecordingId(f"rec_matrix_{case_index:03d}"),
        created_utc_ns=UtcNs(capture_start - 1),
        capture_started_utc_ns=UtcNs(capture_start),
        capture_finished_utc_ns=UtcNs(finish),
        station_id=StationId("station_synthetic_matrix"),
        radio_id=RADIO,
        radio_serial="offline-no-hardware",
        receiver_chain_ids=RECEIVERS,
        clock_status="synthetic-exact",
        hardware_metadata_snapshot_id=HardwareSnapshotId("hw_synthetic_matrix"),
        activities=(
            ActivityManifest(
                activity_request.activity_id,
                activity_request.kind,
                UtcNs(capture_start),
                UtcNs(finish),
                tuple(request.segment_id for request in requests),
            ),
        ),
        segments=manifests,
        plan_id=plan.plan_id,
        producer="offline-detector-matrix",
    )
    data = b"".join(payloads[request.segment_id] for request in requests)
    metadata = canonical_json_bytes(manifest)
    data_ref = ObjectRef(
        Digest.sha256(data),
        len(data),
        "application/vnd.sigmf.data",
        "sigmf-ci16-le-v1",
        f"memory:data:{case.case_id}",
    )
    metadata_ref = ObjectRef(
        Digest.sha256(metadata),
        len(metadata),
        "application/vnd.sigmf.meta+json",
        "sigmf-meta-v1",
        f"memory:metadata:{case.case_id}",
    )
    ref = RecordingObjectRef(
        manifest.recording_id, data_ref, metadata_ref, Digest.sha256(metadata)
    )
    return _MemoryRecordingView(manifest, payloads), ref, capture_start


def _verify_background_lineage(analyzed: tuple[AnalyzedCase, ...]) -> None:
    null_by_group = {
        item.case.group.group_id: item
        for item in analyzed
        if not item.case.condition.signal_present
    }
    if len(null_by_group) != len({item.case.group.group_id for item in analyzed}):
        raise RuntimeError("each background group must have exactly one null recording")
    for item in analyzed:
        null = null_by_group[item.case.group.group_id]
        null_segments = {
            (segment["channel"], segment["edge"]): segment
            for segment in null.truth["segments"]
        }
        for segment in item.truth["segments"]:
            base = null_segments[(segment["channel"], segment["edge"])]
            for injected_rx, base_rx in zip(
                segment["receivers"], base["receivers"], strict=True
            ):
                if (
                    injected_rx["noise_seed_u64"] != base_rx["noise_seed_u64"]
                    or injected_rx["base_noise_ci16_sha256"]
                    != base_rx["base_noise_ci16_sha256"]
                ):
                    raise RuntimeError(
                        "frozen background lineage differs within a group"
                    )
            if not segment["expected_signal_present"] and (
                segment["paired_ci16_sha256"] != base["paired_ci16_sha256"]
            ):
                raise RuntimeError("non-target segments changed from frozen background")


def _candidates(
    analyzed: tuple[AnalyzedCase, ...], spec: MatrixSpec
) -> tuple[DatasetCandidate, ...]:
    null_by_group = {
        item.case.group.group_id: item
        for item in analyzed
        if not item.case.condition.signal_present
    }
    independent = (SUITE_METHOD_ID,) + METHOD_IDS
    output: list[DatasetCandidate] = []
    for item in analyzed:
        base = null_by_group[item.case.group.group_id]
        evidence = LabelEvidence(
            source=LabelSource.INJECTED,
            evidence_digest=item.truth_digest,
            producer_id="starlink-detector-matrix-v1",
            produced_utc_ns=item.captured_utc_ns,
            independent_of_method_ids=independent,
            uncertainty=(
                ("scope", "coded-edge-pilot-approximation"),
                ("condition", item.case.condition.condition_id),
            ),
            base_recording_digest=base.recording_ref.identity_digest(),
            injection_spec_digest=item.truth_digest,
        )
        output.append(
            DatasetCandidate(
                feature_set_id=str(item.feature_ref.feature_set_id),
                feature_set_digest=item.feature_ref.bundle_ref.digest,
                recording_id=str(item.recording_ref.recording_id),
                split_group_id=item.case.group.group_id,
                captured_utc_ns=item.captured_utc_ns,
                radio_id=str(RADIO),
                lnb_ids=("synthetic-if-no-lnb",),
                observation_mode="synthetic-starlink-detector-matrix-4096",
                sample_rate_hz=spec.sample_rate_hz,
                gain_mode="synthetic-explicit-path",
                gain_db=None,
                satellite_id=None,
                truth=TruthLabel(
                    item.case.condition.signal_present,
                    LabelSource.INJECTED,
                    (evidence,),
                    confidence=1.0,
                ),
                derived_from_recording_id=(
                    str(base.recording_ref.recording_id)
                    if item.case.condition.signal_present
                    else None
                ),
            )
        )
    return tuple(output)


def _aggregate_summaries(
    analyzed: tuple[AnalyzedCase, ...],
    thresholds_tuple: tuple[tuple[str, float], ...],
    *,
    converter_min: int,
    converter_max: int,
) -> dict[str, Any]:
    thresholds = dict(thresholds_tuple)
    segment_counts: dict[str, dict[str, Counter[str]]] = {
        method: {split.value: Counter() for split in DatasetSplit}
        for method in METHOD_IDS
    }
    detections: dict[str, dict[str, dict[str, list[bool]]]] = {
        method: {split.value: defaultdict(list) for split in DatasetSplit}
        for method in METHOD_IDS
    }
    achieved: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    peak_rows: list[tuple[int, bool, str]] = []
    for item in analyzed:
        split = item.case.group.split.value
        score_by = {
            (
                f"{score.method_id}@{score.method_version}",
                str(score.segment_id),
            ): score.score
            for score in item.bundle.method_scores
        }
        target_segments = tuple(
            segment
            for segment in item.truth["segments"]
            if segment["expected_signal_present"]
        )
        for method in METHOD_IDS:
            target_fired = False
            for segment in item.truth["segments"]:
                truth = bool(segment["expected_signal_present"])
                fired = score_by[(method, segment["segment_id"])] >= thresholds[method]
                key = (
                    "tp"
                    if truth and fired
                    else "fn"
                    if truth
                    else "fp"
                    if fired
                    else "tn"
                )
                segment_counts[method][split][key] += 1
                target_fired = target_fired or (truth and fired)
            if item.case.condition.signal_present:
                if len(target_segments) != 1:
                    raise RuntimeError(
                        "positive case must have exactly one target segment"
                    )
                detections[method][split][_snr_key(item.case.condition.snr_db)].append(
                    target_fired
                )
        for segment in item.truth["segments"]:
            for receiver in segment["receivers"]:
                peak_rows.append(
                    (
                        int(receiver["peak_component_counts"]),
                        item.case.condition.near_clipping,
                        item.case.condition.condition_id,
                    )
                )
                if int(receiver["clipped_component_count"]) != 0:
                    raise RuntimeError("matrix fixture clipped unexpectedly")
        if item.case.condition.signal_present:
            target = target_segments[0]
            snr_key = _snr_key(item.case.condition.snr_db)
            for receiver in target["receivers"]:
                value = receiver["achieved_prequantization_snr_db"]
                if value is None:
                    raise RuntimeError("positive target lacks achieved SNR")
                achieved[snr_key][receiver["receiver_chain_id"]].append(float(value))

    segment_report: dict[str, Any] = {}
    for method in METHOD_IDS:
        by_split: dict[str, Any] = {}
        overall: Counter[str] = Counter()
        for split in DatasetSplit:
            counts = segment_counts[method][split.value]
            overall.update(counts)
            by_split[split.value] = _counts_json(counts)
        segment_report[method] = {
            "overall": _counts_json(overall),
            "by_split": by_split,
        }
    detection_report: dict[str, Any] = {}
    for method in METHOD_IDS:
        by_split = {
            split.value: _detection_rates(detections[method][split.value])
            for split in DatasetSplit
        }
        overall_trials: dict[str, list[bool]] = defaultdict(list)
        for split in DatasetSplit:
            for snr, values in detections[method][split.value].items():
                overall_trials[snr].extend(values)
        detection_report[method] = {
            "overall": _detection_rates(overall_trials),
            "by_split": by_split,
        }
    achieved_report = {
        snr: {
            receiver: _distribution(values)
            for receiver, values in sorted(receivers.items())
        }
        for snr, receivers in sorted(achieved.items(), key=lambda pair: float(pair[0]))
    }
    near_peaks = [peak for peak, near, _ in peak_rows if near]
    maximum_peak = max(peak for peak, _, _ in peak_rows)
    near_maximum = max(near_peaks)
    return {
        "segment_level_confusion": segment_report,
        "per_snr_detection": detection_report,
        "achieved_rx_snr_db": {
            "basis": "pre-quantization signal/noise power over delayed active coded-pilot samples",
            "by_nominal_source_snr_db": achieved_report,
        },
        "converter_margin": {
            "converter_min": converter_min,
            "converter_max": converter_max,
            "clipped_component_count": 0,
            "maximum_peak_component_counts": maximum_peak,
            "near_clipping_peak_component_counts": near_maximum,
            "near_clipping_headroom_counts": converter_max - near_maximum,
            "near_clipping_peak_fraction_of_positive_limit": near_maximum
            / converter_max,
        },
    }


def _recording_confusion(evaluation: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for method in evaluation.methods:
        by_split = {
            split.split: {
                "true_positive": split.truth.true_positive,
                "false_positive": split.truth.false_positive,
                "true_negative": split.truth.true_negative,
                "false_negative": split.truth.false_negative,
                "scored_prediction_count": split.truth.scored_prediction_count,
                "missing_prediction_count": split.truth.missing_prediction_count,
            }
            for split in method.by_split
        }
        output[method.method_id] = {
            "overall": {
                key: sum(split_counts[key] for split_counts in by_split.values())
                for key in next(iter(by_split.values()))
            },
            "by_split": by_split,
        }
    return output


def _association_json(value: Any) -> dict[str, Any]:
    return {
        "method_ids": value.method_ids,
        "firing_covariance": value.firing_covariance,
        "phi": value.phi,
        "shared_window_count": value.shared_window_count,
        "shared_sample_count": value.shared_sample_count,
        "method_present_window_count": value.method_present_window_count,
        "union_window_count": value.union_window_count,
        "missing_window_count": value.missing_window_count,
    }


def _coverage_report(
    spec: MatrixSpec, cases: tuple[ExpandedCase, ...]
) -> dict[str, Any]:
    split_groups = Counter(group.split.value for group in spec.groups)
    split_recordings = Counter(case.group.split.value for case in cases)
    return {
        "background_group_count": len(spec.groups),
        "recording_count": len(cases),
        "groups_by_split": dict(sorted(split_groups.items())),
        "recordings_by_split": dict(sorted(split_recordings.items())),
        "conditions_per_group": len(spec.conditions),
        "nominal_source_snr_db": sorted(
            condition.snr_db
            for condition in spec.conditions
            if condition.snr_db is not None
        ),
        "edges": sorted({case.edge for case in cases}),
        "pilot_subsets": sorted({case.condition.pilot_subset for case in cases}),
        "cfo_hz": sorted({case.condition.cfo_hz for case in cases}),
        "second_receiver_gain_linear": sorted(
            {case.group.second_receiver.gain_linear for case in cases}
        ),
        "second_receiver_phase_offset_rad": sorted(
            {case.group.second_receiver.phase_offset_rad for case in cases}
        ),
        "second_receiver_integer_delay_samples": sorted(
            {case.group.second_receiver.integer_delay_samples for case in cases}
        ),
        "near_clipping_condition_ids": sorted(
            {
                case.condition.condition_id
                for case in cases
                if case.condition.near_clipping
            }
        ),
    }


def _condition_arms(spec: MatrixSpec) -> dict[str, list[dict[str, Any]]]:
    arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for condition in spec.conditions:
        if not condition.signal_present:
            continue
        arms[_snr_key(condition.snr_db)].append(
            {
                "condition_id": condition.condition_id,
                "pilot_subset": condition.pilot_subset,
                "edge_flip": condition.edge_flip,
                "cfo_hz": condition.cfo_hz,
                "target_channel_offset": condition.target_channel_offset,
                "near_clipping": condition.near_clipping,
            }
        )
    return {snr: arms[snr] for snr in sorted(arms, key=float)}


def _counts_json(counts: Counter[str]) -> dict[str, int]:
    return {
        "true_positive": counts["tp"],
        "false_positive": counts["fp"],
        "true_negative": counts["tn"],
        "false_negative": counts["fn"],
        "target_segment_count": counts["tp"] + counts["fn"],
        "non_target_segment_count": counts["fp"] + counts["tn"],
    }


def _distribution(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "minimum": min(values),
        "mean": math.fsum(values) / len(values),
        "maximum": max(values),
    }


def _detection_rates(values_by_snr: Mapping[str, Sequence[bool]]) -> dict[str, Any]:
    return {
        snr: {
            "detected": sum(values),
            "trials": len(values),
            "fraction": sum(values) / len(values),
        }
        for snr, values in sorted(
            values_by_snr.items(), key=lambda pair: float(pair[0])
        )
    }


def _snr_key(value: float | None) -> str:
    if value is None:
        raise ValueError("null condition has no SNR key")
    return format(value, "g")


def _stabilize_output_size(report: dict[str, Any]) -> None:
    for _ in range(10):
        size = len(_canonical_json(report))
        if report["size"]["output_json_bytes"] == size:
            return
        report["size"]["output_json_bytes"] = size
    raise RuntimeError("output byte count did not stabilize")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, dict):
        raise MatrixSpecificationError(f"{name} must be an object")
    return result


def _token(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if (
        not isinstance(result, str)
        or not result
        or any(char.isspace() for char in result)
    ):
        raise MatrixSpecificationError(f"{name} must be a non-empty token")
    return result


def _integer(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int):
        raise MatrixSpecificationError(f"{name} must be an integer")
    return result


def _positive_int(value: Mapping[str, Any], name: str) -> int:
    result = _integer(value, name)
    if result <= 0:
        raise MatrixSpecificationError(f"{name} must be positive")
    return result


def _nonnegative_int(value: Mapping[str, Any], name: str) -> int:
    result = _integer(value, name)
    if result < 0:
        raise MatrixSpecificationError(f"{name} must be nonnegative")
    return result


def _finite(value: Mapping[str, Any], name: str) -> float:
    result = value.get(name)
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(result)
    ):
        raise MatrixSpecificationError(f"{name} must be finite")
    return float(result)


def _finite_positive(value: Mapping[str, Any], name: str) -> float:
    result = _finite(value, name)
    if result <= 0:
        raise MatrixSpecificationError(f"{name} must be positive")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = load_matrix_spec(args.spec)
    report = run_benchmark(spec)
    payload = _canonical_json(report)
    if len(payload) != report["size"]["output_json_bytes"]:
        raise RuntimeError("final output size differs from aggregate report")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    print(
        f"wrote {args.output} ({len(payload)} bytes, sha256:{digest}, "
        f"{report['runtime']['total_seconds']:.3f}s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
