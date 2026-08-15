"""Bounded, read-only detector audit over the frozen synchronized QNAP subset.

The source corpus is external.  This module validates content-addressed references,
adapts the documented tuning-major CI16 layout to ``RecordingView``, and reports
method firing behavior.  It deliberately has no target-label or accuracy path.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from leo_flow.analysis.dataset import method_firing_association
from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DetectorSuiteConfig,
    IndependentDetectorSuite,
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
)
from leo_flow.capture import StarlinkEdgeScanSpec, build_starlink_edge_scan_plan
from leo_flow.contracts.capture import (
    ActivityManifest,
    GainMode,
    GainSetting,
    RecordingManifest,
    SegmentManifest,
)
from leo_flow.contracts.continuity import ContiguousRfSpan, SafeSampleWindow
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
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
    MethodScore,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.ports import RecordingView

DATASET_SCHEMA = "leo-flow.qnap-synchronised-real-dataset/v1"
REPORT_SCHEMA = "leo-flow.qnap-synchronised-real-detector-report/v1"
SOURCE_SCHEMA = "leo-tracker.interim-synchronised-scan/v1"
DEFAULT_MANIFEST = (
    Path(__file__).with_name("manifests") / "qnap-synchronised-real-development-v1.json"
)
DEFAULT_ROOT = Path("/mnt/qnap01/mouse9911/leo-scans")
METHOD_IDS = (
    "coarse-energy@0.1.0",
    "paired-common-mode@0.1.0",
    "periodic-coherence@0.1.0",
)


class RealDatasetError(ValueError):
    """The frozen manifest or referenced source objects are inconsistent."""


@dataclass(frozen=True)
class DatasetSpec:
    raw: Mapping[str, Any]
    members: tuple[Mapping[str, Any], ...]
    window_starts: tuple[int, ...]
    detector_config: DetectorSuiteConfig
    thresholds: Mapping[str, float]

    @property
    def digest(self) -> Digest:
        return Digest.sha256(_canonical_json(self.raw))


@dataclass(frozen=True)
class _AnalyzedMember:
    member: Mapping[str, Any]
    scores: tuple[MethodScore, ...]
    warnings: tuple[str, ...]
    reason_codes: tuple[str, ...]
    segment_coordinates: Mapping[SegmentId, tuple[int, str]]
    quality: Mapping[str, Any]


class _QnapSparseRecordingView:
    """Read only the predeclared windows from a tuning-major paired CI16 file."""

    def __init__(
        self,
        manifest: RecordingManifest,
        iq_path: Path,
        segment_indices: Mapping[SegmentId, int],
        window_starts: tuple[int, ...],
        window_samples: int,
    ) -> None:
        self._manifest = manifest
        self._iq_path = iq_path
        self._segment_indices = dict(segment_indices)
        self._window_starts = window_starts
        self._window_samples = window_samples

    @property
    def manifest(self) -> RecordingManifest:
        return self._manifest

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes:
        if start_sample not in self._window_starts:
            raise ValueError("window start is not in the frozen selection")
        if stop_sample - start_sample != self._window_samples:
            raise ValueError("window length differs from the frozen selection")
        try:
            tuning_index = self._segment_indices[segment_id]
        except KeyError as error:
            raise ValueError("unknown segment") from error
        segment_samples = self._manifest.segments[tuning_index].sample_count
        byte_offset = (tuning_index * segment_samples + start_sample) * 8
        expected = self._window_samples * 8
        with self._iq_path.open("rb") as stream:
            stream.seek(byte_offset)
            payload = stream.read(expected)
        if len(payload) != expected:
            raise ValueError("source IQ ended inside a selected window")
        return payload

    def continuity(self, segment_id: SegmentId) -> None:
        if segment_id not in self._segment_indices:
            raise KeyError(segment_id)
        # The source has no refill-level continuity proof.

    def contiguous_rf_spans(
        self, segment_id: SegmentId
    ) -> tuple[ContiguousRfSpan, ...]:
        index = self._segment_indices[segment_id]
        count = self._manifest.segments[index].sample_count
        return (ContiguousRfSpan(0, count, 0, count),)

    def iter_safe_windows(
        self, segment_id: SegmentId, window_samples: int, stride_samples: int
    ) -> Iterator[SafeSampleWindow]:
        del stride_samples
        if segment_id not in self._segment_indices:
            raise KeyError(segment_id)
        if window_samples != self._window_samples:
            raise ValueError("detector window differs from the frozen selection")
        yield from (
            SafeSampleWindow(start, start + window_samples)
            for start in self._window_starts
        )


def load_dataset_spec(path: Path = DEFAULT_MANIFEST) -> DatasetSpec:
    """Load and strictly validate the compact, external-reference-only manifest."""

    try:
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RealDatasetError(f"cannot load dataset manifest: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != DATASET_SCHEMA:
        raise RealDatasetError("unsupported dataset schema")
    members_value = raw.get("members")
    if not isinstance(members_value, list) or not members_value:
        raise RealDatasetError("members must be a non-empty array")
    members = tuple(_as_mapping(member, "member") for member in members_value)
    expected_membership = hashlib.sha256(_canonical_json(members_value)).hexdigest()
    if raw.get("membership_digest_sha256") != expected_membership:
        raise RealDatasetError("membership digest does not match members")
    member_ids = [_token(member, "member_id") for member in members]
    recording_ids = [_token(member, "recording_id") for member in members]
    if len(member_ids) != len(set(member_ids)) or len(recording_ids) != len(
        set(recording_ids)
    ):
        raise RealDatasetError("member and recording identities must be unique")
    group = _mapping(raw, "partition_policy").get("split_group_id")
    for member in members:
        if member.get("partition") != "development":
            raise RealDatasetError("real corpus members must remain development-only")
        if member.get("split_group_id") != group:
            raise RealDatasetError("all members must remain in one conservative group")
        truth = _mapping(member, "truth")
        if (
            truth.get("tier") != "unlabeled_sky"
            or truth.get("target_present") is not None
            or truth.get("accuracy_eligible") is not False
        ):
            raise RealDatasetError("real members must remain accuracy-ineligible")
        for ref_name in ("source_metadata_ref", "iq_ref"):
            ref = _mapping(member, ref_name)
            _relative_path(ref, "relative_path")
            _sha256(ref, "sha256")
            _positive_int(ref, "bytes")
        iq = _mapping(member, "iq_ref")
        if iq.get("dtype") != "<i2" or iq.get("layout") != [
            "tuning",
            "sample",
            "receiver",
            "component",
        ]:
            raise RealDatasetError("unsupported IQ representation")
        shape = iq.get("shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, int) for value in shape
            )
            or shape[0] != 8
            or shape[2:] != [2, 2]
            or math.prod(shape) * 2 != iq["bytes"]
        ):
            raise RealDatasetError("IQ shape and byte count differ")
    selection = _mapping(raw, "selection")
    starts_value = selection.get("representative_window_starts")
    if not isinstance(starts_value, list) or not starts_value:
        raise RealDatasetError("representative windows are required")
    starts = tuple(
        _nonnegative_scalar_int(value, "window start") for value in starts_value
    )
    if starts != tuple(sorted(set(starts))):
        raise RealDatasetError("representative windows must be sorted and unique")
    profile = _mapping(raw, "analysis_profile")
    config_value = _mapping(profile, "config")
    config = DetectorSuiteConfig(
        window_samples=_positive_int(config_value, "window_samples"),
        stride_samples=_positive_int(config_value, "stride_samples"),
        periodic_lag_samples=_positive_int(config_value, "periodic_lag_samples"),
        max_pair_delay_samples=_nonnegative_int(config_value, "max_pair_delay_samples"),
        clip_threshold_abs=_positive_int(config_value, "clip_threshold_abs"),
        refuse_clipping=config_value.get("refuse_clipping") is True,
    )
    if any(
        start + config.window_samples > member["iq_ref"]["shape"][1]
        for member in members
        for start in starts
    ):
        raise RealDatasetError("representative window lies outside source IQ")
    threshold_values = _mapping(_mapping(profile, "reference_thresholds"), "values")
    thresholds = {method: _finite(threshold_values, method) for method in METHOD_IDS}
    if set(threshold_values) != set(METHOD_IDS):
        raise RealDatasetError("reference thresholds must name exactly current methods")
    expected_window_count = len(members) * 8 * len(starts)
    if selection.get("detector_window_count") != expected_window_count:
        raise RealDatasetError("declared detector window count differs")
    expected_bytes = expected_window_count * config.window_samples * 8
    if selection.get("analyzed_iq_bytes") != expected_bytes:
        raise RealDatasetError("declared analyzed IQ bytes differ")
    return DatasetSpec(raw, members, starts, config, thresholds)


def verify_references(
    spec: DatasetSpec, root: Path, *, verify_full_iq: bool
) -> dict[str, Any]:
    """Verify selected source metadata and optionally every selected IQ byte."""

    metadata_paths: set[str] = set()
    verified_iq_bytes = 0
    for member in spec.members:
        metadata_ref = _mapping(member, "source_metadata_ref")
        metadata_relative = _relative_path(metadata_ref, "relative_path")
        if metadata_relative not in metadata_paths:
            _verify_object(root, metadata_ref, hash_content=True)
            metadata_paths.add(metadata_relative)
        iq_ref = _mapping(member, "iq_ref")
        _verify_object(root, iq_ref, hash_content=verify_full_iq)
        if verify_full_iq:
            verified_iq_bytes += int(iq_ref["bytes"])
    return {
        "metadata_object_count": len(metadata_paths),
        "metadata_hashes_verified": True,
        "iq_object_count": len(spec.members),
        "full_iq_hashes_verified": verify_full_iq,
        "full_iq_bytes_hashed": verified_iq_bytes,
    }


def run_detector_audit(spec: DatasetSpec, root: Path) -> dict[str, Any]:
    """Analyze only frozen windows and return label-free behavior summaries."""

    analyzer = IndependentDetectorSuite(
        spec.detector_config,
        AnalysisExecutionContext(
            producer_name="qnap-real-dataset-audit",
            producer_version="1",
            git_commit="offline-benchmark",
            environment_digest=canonical_digest(
                {"mode": "read-only-external-recording", "dataset": str(spec.digest)}
            ),
            started_utc_ns=UtcNs(1_900_000_000_000_000_000),
            completed_utc_ns=UtcNs(1_900_000_000_000_000_001),
            host_class="offline-benchmark-host",
        ),
    )
    analyzed: list[_AnalyzedMember] = []
    all_scores: list[MethodScore] = []
    for member in spec.members:
        view, recording_ref, coordinates = _recording_view(spec, member, root)
        request = RecordingAnalysisRequest(
            schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
            recording_id=recording_ref.recording_id,
            recording_object_ref=recording_ref,
            algorithm_ref=detector_suite_algorithm_ref(),
            config_ref=detector_suite_config_ref(spec.detector_config),
            dependency_refs=(
                ArtifactRef(
                    f"source-sweep-{member['sweep_pair_id']}",
                    _digest(_mapping(member, "source_metadata_ref")["sha256"]),
                    SchemaRef("leo-tracker.interim-synchronised-scan"),
                ),
            ),
            requested_output_schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
        quality = _selected_window_quality(view, spec)
        bundle = analyzer.analyze(cast(RecordingView, view), request)
        analyzed.append(
            _AnalyzedMember(
                member,
                bundle.method_scores,
                bundle.warnings,
                bundle.reason_codes,
                coordinates,
                quality,
            )
        )
        all_scores.extend(bundle.method_scores)
    association = method_firing_association(all_scores, spec.thresholds)
    expected_windows = len(spec.members) * 8 * len(spec.window_starts)
    observed_by_method = Counter(
        f"{score.method_id}@{score.method_version}" for score in all_scores
    )
    firing_counts = Counter(
        method
        for score in all_scores
        if (method := f"{score.method_id}@{score.method_version}")
        and score.score >= spec.thresholds[method]
    )
    return {
        "schema": REPORT_SCHEMA,
        "offline_only": True,
        "dataset": {
            "dataset_id": spec.raw["dataset_id"],
            "manifest_digest": str(spec.digest),
            "membership_digest_sha256": spec.raw["membership_digest_sha256"],
            "partition": "development",
            "split_group_ids": sorted(
                {str(member["split_group_id"]) for member in spec.members}
            ),
            "target_truth_status": "absent",
            "accuracy_eligible": False,
        },
        "scope": {
            "recording_count": len(spec.members),
            "sweep_count": len({member["sweep_pair_id"] for member in spec.members}),
            "tuning_count": len(spec.members) * 8,
            "selected_window_attempt_count": expected_windows,
            "selected_iq_bytes_read_by_detector": (
                expected_windows * spec.detector_config.window_samples * 8
            ),
            "accepted_window_count_by_method": {
                method: observed_by_method[method] for method in METHOD_IDS
            },
            "refused_window_count_by_method": {
                method: expected_windows - observed_by_method[method]
                for method in METHOD_IDS
            },
        },
        "reference_thresholds": {
            "role": "exploratory firing summaries only",
            "fitted_on_real_corpus": False,
            "values": dict(sorted(spec.thresholds.items())),
        },
        "firings": {
            method: {
                "count": firing_counts[method],
                "accepted_window_count": observed_by_method[method],
                "fraction_of_accepted_windows": (
                    firing_counts[method] / observed_by_method[method]
                    if observed_by_method[method]
                    else None
                ),
            }
            for method in METHOD_IDS
        },
        "score_distributions": _score_distributions(all_scores),
        "method_firing_association": _association_json(association),
        "cross_radio_direct_replication": _cross_radio_agreement(
            analyzed, spec.thresholds, spec.window_starts
        ),
        "quality": {
            "by_recording": [
                {
                    "member_id": item.member["member_id"],
                    **item.quality,
                    "detector_warnings": list(item.warnings),
                    "detector_reason_codes": list(item.reason_codes),
                }
                for item in analyzed
            ],
            "interpretation": (
                "Clipping refusal is detector policy, not a target label. Source "
                "metadata provides no refill-level continuity proof."
            ),
        },
        "scientific_limitations": [
            "No independent target-present or target-absent labels exist; no confusion matrix or accuracy metric is computed.",
            "Reference thresholds were calibrated on synthetic TRAIN data and are used only to describe firing behavior on this development corpus.",
            "The source records tuning order but not numeric tuned frequencies, analog bandwidth, exact segment start timestamps, or refill-level continuity; the adapter uses the repository scan-plan frequencies and synthetic segment timestamps only to satisfy detector input contracts.",
            "Only four score-blind windows from each tuning of three sweeps are analyzed; results do not estimate full-corpus rates.",
            "Same-order cross-radio agreement is a replication proxy, not independent truth. Opposite-order sweeps are excluded from direct same-hypothesis agreement because corresponding tuning indices observe different edges.",
            "The source README reports an lnb-a hardware concern; that operator note is not independently verified and affects every pluto-5d4d member in this subset.",
        ],
    }


def _recording_view(
    spec: DatasetSpec, member: Mapping[str, Any], root: Path
) -> tuple[
    _QnapSparseRecordingView, RecordingObjectRef, Mapping[SegmentId, tuple[int, str]]
]:
    metadata_ref = _mapping(member, "source_metadata_ref")
    source = json.loads(
        _safe_path(root, _relative_path(metadata_ref, "relative_path")).read_bytes()
    )
    if source.get("schema") != SOURCE_SCHEMA:
        raise RealDatasetError("source sweep has unsupported schema")
    radio_name = _token(member, "radio_id")
    radio = _mapping(_mapping(source, "radios"), radio_name)
    iq = _mapping(radio, "iq")
    iq_ref = _mapping(member, "iq_ref")
    expected_iq_path = Path(str(member["sweep_pair_id"])) / str(iq["path"])
    if (
        source.get("utc") != member.get("utc")
        or source.get("sweep") != member.get("sweep_number")
        or radio.get("error") is not None
        or radio.get("edge_order") != member.get("edge_order")
        or radio.get("receiver_labels") != member.get("receiver_labels")
        or iq.get("bytes") != iq_ref.get("bytes")
        or iq.get("shape") != iq_ref.get("shape")
        or expected_iq_path.as_posix() != iq_ref.get("relative_path")
    ):
        raise RealDatasetError(f"source metadata differs for {member['member_id']}")
    arm = _mapping(radio, "arm")
    sample_rate = _finite(arm, "sample_rate_hz")
    shape = cast(list[int], iq["shape"])
    receiver_labels = cast(list[str], member["receiver_labels"])
    plan_id = PlanId(f"plan_{str(member['member_id']).replace('-', '_')}")
    radio_id = RadioId(f"radio_{radio_name.replace('-', '_')}")
    receivers = tuple(
        ReceiverChainId(f"rx_{label.replace('-', '_')}") for label in receiver_labels
    )
    plan = build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=plan_id,
            radio_id=radio_id,
            receiver_chain_ids=cast(tuple[ReceiverChainId, ReceiverChainId], receivers),
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=sample_rate,
            bandwidth_hz=sample_rate,
            sample_count=shape[1],
            edge_order=str(member["edge_order"]),
            arm_name=str(arm["name"]),
            hardware_block_samples=shape[1],
        )
    )
    requests = tuple(
        segment for activity in plan.activities for segment in activity.segments
    )
    declared_tunings = tuple((int(item[0]), str(item[1])) for item in radio["tunings"])
    planned_tunings = tuple(
        (int(dict(request.tags)["channel"]), str(dict(request.tags)["edge"]))
        for request in requests
    )
    if declared_tunings != planned_tunings:
        raise RealDatasetError("declared tuning order differs from scan-plan adapter")
    base_ns = _utc_ns(str(member["utc"]))
    duration_ns = round(shape[1] * 1_000_000_000 / sample_rate)
    segments = tuple(
        SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=request.center_frequency_hz,
            actual_sample_rate_hz=sample_rate,
            actual_bandwidth_hz=sample_rate,
            actual_gain=request.gain,
            start_utc_ns=UtcNs(base_ns + index * duration_ns),
            monotonic_start_ns=index * duration_ns,
            sample_count=shape[1],
            shape=(shape[1], 2, 2),
            diagnostics=(("timing_status", "analysis-only-inferred"),),
        )
        for index, request in enumerate(requests)
    )
    finished_ns = base_ns + len(segments) * duration_ns
    activity = plan.activities[0]
    manifest = RecordingManifest(
        schema=SchemaRef(RecordingManifest.SCHEMA_ID),
        recording_id=RecordingId(str(member["recording_id"])),
        created_utc_ns=UtcNs(base_ns),
        capture_started_utc_ns=UtcNs(base_ns),
        capture_finished_utc_ns=UtcNs(finished_ns),
        station_id=StationId("station_qnap_unverified"),
        radio_id=radio_id,
        radio_serial=radio_name,
        receiver_chain_ids=receivers,
        clock_status="source-barrier-skew-only",
        hardware_metadata_snapshot_id=HardwareSnapshotId(
            f"hw_qnap_unverified_{radio_name.replace('-', '_')}"
        ),
        activities=(
            ActivityManifest(
                activity.activity_id,
                activity.kind,
                UtcNs(base_ns),
                UtcNs(finished_ns),
                tuple(segment.segment_id for segment in segments),
            ),
        ),
        segments=segments,
        plan_id=plan_id,
        producer="qnap-real-dataset-adapter-v1",
        experiment_tags=(
            ("absolute_frequency_status", "analysis-only-inferred-from-scan-policy"),
            ("source_schema", SOURCE_SCHEMA),
            ("source_sweep_pair_id", member["sweep_pair_id"]),
            ("timing_status", "analysis-only-inferred"),
        ),
    )
    adapted_metadata = canonical_json_bytes(manifest)
    iq_path = _safe_path(root, _relative_path(iq_ref, "relative_path"))
    recording_ref = RecordingObjectRef(
        manifest.recording_id,
        ObjectRef(
            _digest(str(iq_ref["sha256"])),
            int(iq_ref["bytes"]),
            "application/octet-stream",
            "tuning-major-paired-ci16-le-v1",
            f"external:{iq_ref['relative_path']}",
        ),
        ObjectRef(
            Digest.sha256(adapted_metadata),
            len(adapted_metadata),
            "application/json",
            "analysis-adapted-recording-manifest-v1",
            f"memory:adapted:{member['member_id']}",
        ),
        canonical_digest(manifest),
    )
    coordinates = {
        segment.segment_id: declared_tunings[index]
        for index, segment in enumerate(segments)
    }
    indices = {segment.segment_id: index for index, segment in enumerate(segments)}
    return (
        _QnapSparseRecordingView(
            manifest,
            iq_path,
            indices,
            spec.window_starts,
            spec.detector_config.window_samples,
        ),
        recording_ref,
        coordinates,
    )


def _selected_window_quality(
    view: _QnapSparseRecordingView, spec: DatasetSpec
) -> dict[str, Any]:
    component_count = 0
    clipping_component_count = 0
    clipping_window_count = 0
    maximum_abs = 0
    for segment in view.manifest.segments:
        for start in spec.window_starts:
            payload = view.read_iq_bytes(
                segment.segment_id, start, start + spec.detector_config.window_samples
            )
            values = array.array("h")
            values.frombytes(payload)
            if sys.byteorder != "little":
                values.byteswap()
            clipped = sum(
                abs(value) >= spec.detector_config.clip_threshold_abs
                for value in values
            )
            clipping_component_count += clipped
            clipping_window_count += bool(clipped)
            component_count += len(values)
            maximum_abs = max(maximum_abs, max(map(abs, values)))
    return {
        "selected_component_count": component_count,
        "maximum_absolute_component": maximum_abs,
        "clipping_component_count": clipping_component_count,
        "clipping_window_count": clipping_window_count,
    }


def _score_distributions(scores: Sequence[MethodScore]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for score in scores:
        grouped[f"{score.method_id}@{score.method_version}"].append(score.score)
    return {
        method: (
            {
                "count": len(values),
                "minimum": min(values),
                "median": statistics.median(values),
                "maximum": max(values),
            }
            if values
            else {"count": 0, "minimum": None, "median": None, "maximum": None}
        )
        for method in METHOD_IDS
        for values in (grouped[method],)
    }


def _association_json(report: Any) -> dict[str, Any]:
    return {
        "method_ids": list(report.method_ids),
        "firing_covariance": [list(row) for row in report.firing_covariance],
        "phi": [list(row) for row in report.phi],
        "shared_window_count": [list(row) for row in report.shared_window_count],
        "shared_sample_count": [list(row) for row in report.shared_sample_count],
        "method_present_window_count": list(report.method_present_window_count),
        "union_window_count": report.union_window_count,
        "missing_window_count": list(report.missing_window_count),
    }


def _cross_radio_agreement(
    analyzed: Sequence[_AnalyzedMember],
    thresholds: Mapping[str, float],
    starts: tuple[int, ...],
) -> dict[str, Any]:
    by_sweep: dict[str, list[_AnalyzedMember]] = defaultdict(list)
    for item in analyzed:
        by_sweep[str(item.member["sweep_pair_id"])].append(item)
    direct: dict[str, Counter[str]] = {method: Counter() for method in METHOD_IDS}
    included_sweeps: list[str] = []
    excluded_sweeps: list[dict[str, str]] = []
    comparison_radio_order: tuple[str, str] | None = None
    for sweep, pair in sorted(by_sweep.items()):
        if len(pair) != 2:
            raise RealDatasetError("selected sweep does not contain exactly two radios")
        relationship = str(pair[0].member["order_relationship"])
        if relationship == "opposite":
            excluded_sweeps.append(
                {
                    "sweep_pair_id": sweep,
                    "reason": "opposite tuning order is not direct same-hypothesis replication",
                }
            )
            continue
        included_sweeps.append(sweep)
        sorted_pair = sorted(pair, key=lambda value: str(value.member["radio_id"]))
        radios = cast(
            tuple[str, str], tuple(str(item.member["radio_id"]) for item in sorted_pair)
        )
        if comparison_radio_order is not None and radios != comparison_radio_order:
            raise RealDatasetError("direct-comparison radio membership changed")
        comparison_radio_order = radios
        score_maps = []
        for item in sorted_pair:
            score_maps.append(
                {
                    (
                        f"{score.method_id}@{score.method_version}",
                        *item.segment_coordinates[score.segment_id],
                        score.window_start_sample,
                    ): score.score
                    >= thresholds[f"{score.method_id}@{score.method_version}"]
                    for score in item.scores
                }
            )
        for method in METHOD_IDS:
            for channel in range(1, 5):
                for edge in ("lower", "upper"):
                    for start in starts:
                        key = (method, channel, edge, start)
                        left = score_maps[0].get(key)
                        right = score_maps[1].get(key)
                        if left is None or right is None:
                            direct[method]["unavailable"] += 1
                        elif left and right:
                            direct[method]["both_fire"] += 1
                        elif left:
                            direct[method]["left_only"] += 1
                        elif right:
                            direct[method]["right_only"] += 1
                        else:
                            direct[method]["neither"] += 1
    by_method: dict[str, Any] = {}
    for method in METHOD_IDS:
        counts = direct[method]
        compared = sum(
            counts[name] for name in ("both_fire", "left_only", "right_only", "neither")
        )
        by_method[method] = {
            "potential_comparison_count": compared + counts["unavailable"],
            "compared_count": compared,
            "unavailable_count": counts["unavailable"],
            "both_fire": counts["both_fire"],
            "left_only": counts["left_only"],
            "right_only": counts["right_only"],
            "neither": counts["neither"],
            "exact_agreement_fraction": (
                (counts["both_fire"] + counts["neither"]) / compared
                if compared
                else None
            ),
        }
    return {
        "basis": "same sweep, same tuning order, channel, edge, and sample window",
        "radio_order": list(comparison_radio_order or ()),
        "included_sweep_pair_ids": included_sweeps,
        "excluded_sweeps": excluded_sweeps,
        "by_method": by_method,
    }


def _verify_object(root: Path, ref: Mapping[str, Any], *, hash_content: bool) -> None:
    path = _safe_path(root, _relative_path(ref, "relative_path"))
    try:
        size = path.stat().st_size
    except OSError as error:
        raise RealDatasetError(f"cannot stat {path}: {error}") from error
    if size != ref["bytes"]:
        raise RealDatasetError(f"byte count differs for {path}")
    if hash_content:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != ref["sha256"]:
            raise RealDatasetError(f"SHA-256 differs for {path}")


def _safe_path(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise RealDatasetError("reference escapes the configured root") from error
    return resolved


def _utc_ns(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise RealDatasetError(f"invalid source UTC: {value}") from error
    return round(parsed.timestamp() * 1_000_000_000)


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _digest(value: str) -> Digest:
    return Digest(DigestAlgorithm.SHA256, value)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get(name), dict):
        raise RealDatasetError(f"{name} must be an object")
    return value[name]


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RealDatasetError(f"{name} must be an object")
    return value


def _token(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if (
        not isinstance(result, str)
        or not result
        or any(char.isspace() for char in result)
    ):
        raise RealDatasetError(f"{name} must be a non-empty token")
    return result


def _sha256(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if (
        not isinstance(result, str)
        or len(result) != 64
        or any(char not in "0123456789abcdef" for char in result)
    ):
        raise RealDatasetError(f"{name} must be a lowercase SHA-256")
    return result


def _relative_path(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if (
        not isinstance(result, str)
        or not result
        or Path(result).is_absolute()
        or ".." in Path(result).parts
    ):
        raise RealDatasetError(f"{name} must be a safe relative path")
    return result


def _positive_int(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise RealDatasetError(f"{name} must be a positive integer")
    return result


def _nonnegative_int(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise RealDatasetError(f"{name} must be a nonnegative integer")
    return result


def _nonnegative_scalar_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RealDatasetError(f"{name} must be a nonnegative integer")
    return value


def _finite(value: Mapping[str, Any], name: str) -> float:
    result = value.get(name)
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(result)
    ):
        raise RealDatasetError(f"{name} must be finite")
    return float(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--verify-full-iq",
        action="store_true",
        help="hash all 76.8 MB of selected external IQ before analysis",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = load_dataset_spec(args.manifest)
    verification = verify_references(
        spec, args.root, verify_full_iq=args.verify_full_iq
    )
    report = run_detector_audit(spec, args.root)
    report["source_verification"] = verification
    payload = _canonical_json(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(
        f"wrote {args.output} ({len(payload)} bytes, "
        f"sha256:{hashlib.sha256(payload).hexdigest()})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
