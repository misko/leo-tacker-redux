"""Pure offline quality and compact-PSD recording analyzer v0.1."""

from __future__ import annotations

import math
from dataclasses import dataclass

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureId,
    FeatureSetId,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    MethodScore,
    RecordingAnalysisRequest,
)
from leo_flow.storage.ports import RecordingView

from .psd import compact_psd
from .quality import Ci16DecodeError, ReceiverQualityAccumulator, decode_ci16

ALGORITHM_ID = "quality-compact-psd"
ALGORITHM_VERSION = "0.1.0"
QUALITY_METHOD_ID = "sample-quality"
PSD_METHOD_ID = "compact-psd"
CONFIG_SCHEMA_ID = "org.leo-flow.quality-psd-config"


class AnalysisInputError(ValueError):
    """Recording bytes or manifest are inconsistent with the frozen contract."""


class AnalysisConfigurationError(ValueError):
    """The explicit request does not identify this analyzer/configuration."""


@dataclass(frozen=True)
class QualityPsdConfig:
    psd_window_samples: int = 256
    psd_stride_samples: int = 1_000_000
    clip_threshold_abs: int = 2040
    dc_warning_fraction: float = 0.25
    noise_floor_epsilon: float = 1e-12

    def __post_init__(self) -> None:
        if (
            isinstance(self.psd_window_samples, bool)
            or not isinstance(self.psd_window_samples, int)
            or self.psd_window_samples < 8
            or (self.psd_window_samples & (self.psd_window_samples - 1))
        ):
            raise ValueError("psd_window_samples must be a power of two >= 8")
        if (
            isinstance(self.psd_stride_samples, bool)
            or not isinstance(self.psd_stride_samples, int)
            or self.psd_stride_samples <= 0
        ):
            raise ValueError("psd_stride_samples must be positive")
        if (
            isinstance(self.clip_threshold_abs, bool)
            or not isinstance(self.clip_threshold_abs, int)
            or not 1 <= self.clip_threshold_abs <= 32768
        ):
            raise ValueError("clip_threshold_abs must lie in [1, 32768]")
        if (
            isinstance(self.dc_warning_fraction, bool)
            or not isinstance(self.dc_warning_fraction, (int, float))
            or not math.isfinite(self.dc_warning_fraction)
            or not 0.0 <= self.dc_warning_fraction <= 1.0
        ):
            raise ValueError("dc_warning_fraction must lie in [0, 1]")
        if (
            isinstance(self.noise_floor_epsilon, bool)
            or not isinstance(self.noise_floor_epsilon, (int, float))
            or not math.isfinite(self.noise_floor_epsilon)
            or self.noise_floor_epsilon <= 0.0
        ):
            raise ValueError("noise_floor_epsilon must be positive")


@dataclass(frozen=True)
class AnalysisExecutionContext:
    producer_name: str
    producer_version: str
    git_commit: str
    environment_digest: Digest
    started_utc_ns: UtcNs
    completed_utc_ns: UtcNs
    host_class: str

    def __post_init__(self) -> None:
        # Provenance owns canonical validation. Constructing it here catches an
        # invalid execution context before any recording bytes are read.
        Provenance(
            self.producer_name,
            self.producer_version,
            self.git_commit,
            self.environment_digest,
            Digest.sha256(b"context-validation"),
            (Digest.sha256(b"context-validation-input"),),
            (),
            self.started_utc_ns,
            self.completed_utc_ns,
            self.host_class,
        )


def quality_psd_algorithm_ref() -> ArtifactRef:
    descriptor = {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "input": "org.leo-flow.recording-view/v0.1",
        "output": f"{FeatureSetBundle.SCHEMA_ID}/0.1",
        "methods": [QUALITY_METHOD_ID, PSD_METHOD_ID],
        "psd": "rectangular-demeaned-radix2-fft",
    }
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(descriptor),
        SchemaRef("org.leo-flow.recording-algorithm"),
    )


def quality_psd_config_ref(config: QualityPsdConfig) -> ArtifactRef:
    return ArtifactRef(
        "quality-psd-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


def _derived_id(prefix: str, identity: object) -> str:
    return f"{prefix}_{canonical_digest(identity).value[:32]}"


def _midpoint_utc_ns(segment: SegmentManifest, start: int, stop: int) -> UtcNs:
    midpoint_sample = (start + stop) / 2.0
    offset_ns = round(midpoint_sample * 1_000_000_000 / segment.actual_sample_rate_hz)
    return UtcNs(segment.start_utc_ns + offset_ns)


def _window_starts(sample_count: int, window: int, stride: int) -> tuple[int, ...]:
    if sample_count < window:
        return ()
    starts = list(range(0, sample_count - window + 1, stride))
    last = sample_count - window
    if starts[-1] != last:
        starts.append(last)
    return tuple(starts)


def _safe_analysis_spans(
    recording: RecordingView, segment: SegmentManifest
) -> tuple[tuple[int, int], ...]:
    continuity_method = getattr(recording, "continuity", None)
    if not callable(continuity_method):
        return ((0, segment.sample_count),)
    continuity = continuity_method(segment.segment_id)
    if continuity is None or not continuity.is_verified:
        # Legacy and explicitly unverified recordings remain analyzable, but only
        # V5 evidence can split them into proven source-contiguous spans.
        return ((0, segment.sample_count),)
    return tuple(
        (span.start_sample, span.stop_sample)
        for span in continuity.contiguous_rf_spans()
    )


def _safe_window_starts(
    spans: tuple[tuple[int, int], ...], window: int, stride: int
) -> tuple[int, ...]:
    starts: list[int] = []
    for span_start, span_stop in spans:
        starts.extend(
            span_start + relative
            for relative in _window_starts(span_stop - span_start, window, stride)
        )
    return tuple(starts)


class QualityPsdAnalyzer:
    """Analyze exactly one supplied recording with no external capabilities."""

    def __init__(
        self,
        config: QualityPsdConfig,
        execution: AnalysisExecutionContext,
        *,
        read_chunk_samples: int = 65_536,
    ) -> None:
        if (
            isinstance(read_chunk_samples, bool)
            or not isinstance(read_chunk_samples, int)
            or read_chunk_samples <= 0
        ):
            raise ValueError("read_chunk_samples must be positive")
        self._config = config
        self._execution = execution
        self._read_chunk_samples = read_chunk_samples

    @property
    def maximum_read_samples(self) -> int:
        return max(self._read_chunk_samples, self._config.psd_window_samples)

    def analyze(
        self, recording: RecordingView, request: RecordingAnalysisRequest
    ) -> FeatureSetBundle:
        manifest = recording.manifest
        if manifest.recording_id != request.recording_id:
            raise AnalysisInputError("recording view and request IDs differ")
        if request.requested_output_schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            raise AnalysisConfigurationError(
                "request does not ask for FeatureSetBundle v0.1"
            )
        expected_algorithm = quality_psd_algorithm_ref()
        if request.algorithm_ref != expected_algorithm:
            raise AnalysisConfigurationError(
                "request algorithm_ref does not identify this analyzer"
            )
        expected_config = quality_psd_config_ref(self._config)
        if request.config_ref != expected_config:
            raise AnalysisConfigurationError(
                "request config_ref does not match analyzer config"
            )
        dependency_keys = [
            dependency.artifact_id for dependency in request.dependency_refs
        ]
        if len(dependency_keys) != len(set(dependency_keys)):
            raise AnalysisConfigurationError("dependency_refs contain duplicates")
        dependencies = tuple(
            sorted(
                request.dependency_refs,
                key=lambda ref: (ref.artifact_id, str(ref.digest)),
            )
        )
        recording_digest = request.recording_object_ref.identity_digest()
        run_identity = {
            "recording_id": str(request.recording_id),
            "recording_identity_digest": str(recording_digest),
            "algorithm_digest": str(request.algorithm_ref.digest),
            "config_digest": str(request.config_ref.digest),
            "dependency_digests": [str(ref.digest) for ref in dependencies],
            "environment_digest": str(self._execution.environment_digest),
            "git_commit": self._execution.git_commit,
        }
        run_token = canonical_digest(run_identity).value
        analysis_run_id = AnalysisRunId(f"arun_{run_token[:32]}")
        feature_set_id = FeatureSetId(f"fset_{run_token[32:64]}")
        observations: list[FeatureObservation] = []
        method_scores: list[MethodScore] = []
        warnings: list[str] = []
        reason_codes: set[str] = set()

        for segment in manifest.segments:
            receiver_ids = segment.requested.receiver_chain_ids
            receiver_count = len(receiver_ids)
            spans = _safe_analysis_spans(recording, segment)
            if len(spans) > 1:
                warnings.append(
                    f"{segment.segment_id}:verified-gapped:{len(spans) - 1}-gaps"
                )
                reason_codes.add("verified-source-gaps")
            for span_start, span_stop in spans:
                accumulators = [ReceiverQualityAccumulator() for _ in receiver_ids]
                for start in range(span_start, span_stop, self._read_chunk_samples):
                    stop = min(span_stop, start + self._read_chunk_samples)
                    raw = self._read_exact(
                        recording, segment.segment_id, start, stop, receiver_count
                    )
                    try:
                        values, decoded_samples = decode_ci16(raw, receiver_count)
                    except Ci16DecodeError as exc:
                        raise AnalysisInputError(
                            f"segment {segment.segment_id} has malformed CI16: {exc}"
                        ) from exc
                    if decoded_samples != stop - start:
                        raise AnalysisInputError(
                            f"segment {segment.segment_id} decoded sample count differs"
                        )
                    for receiver_index, accumulator in enumerate(accumulators):
                        accumulator.consume(
                            values,
                            receiver_index=receiver_index,
                            receiver_count=receiver_count,
                            sample_count=decoded_samples,
                            clip_threshold_abs=self._config.clip_threshold_abs,
                        )
                for receiver_id, accumulator in zip(
                    receiver_ids, accumulators, strict=True
                ):
                    quality = accumulator.summary(
                        dc_warning_fraction=self._config.dc_warning_fraction
                    )
                    feature_id = FeatureId(
                        _derived_id(
                            "feature",
                            {
                                "run": run_token,
                                "method": QUALITY_METHOD_ID,
                                "segment": str(segment.segment_id),
                                "receiver": str(receiver_id),
                                "start": span_start,
                                "stop": span_stop,
                            },
                        )
                    )
                    observations.append(
                        FeatureObservation(
                            feature_id=feature_id,
                            recording_id=request.recording_id,
                            segment_id=segment.segment_id,
                            method_id=QUALITY_METHOD_ID,
                            method_version=ALGORITHM_VERSION,
                            window_start_sample=span_start,
                            window_stop_sample=span_stop,
                            segment_sample_count=segment.sample_count,
                            midpoint_utc_ns=_midpoint_utc_ns(
                                segment, span_start, span_stop
                            ),
                            feature_kind="sample-quality",
                            score=quality.rms_magnitude,
                            score_semantics="rms-magnitude-counts",
                            receiver_chain_id=receiver_id,
                            noise_estimate=quality.ac_power,
                            uncertainty=(("status", "descriptive-only"),),
                            quality_flags=quality.flags,
                            diagnostics=quality.diagnostics(),
                        )
                    )
                    warnings.extend(
                        f"{segment.segment_id}:{receiver_id}:{flag}"
                        for flag in quality.flags
                    )
                    if quality.flags:
                        reason_codes.add("quality-flags-present")

            starts = _safe_window_starts(
                spans,
                self._config.psd_window_samples,
                self._config.psd_stride_samples,
            )
            if not starts:
                warnings.append(f"{segment.segment_id}:segment-too-short-for-psd")
                reason_codes.add("psd-skipped-short-segment")
                continue
            for start in starts:
                stop = start + self._config.psd_window_samples
                raw = self._read_exact(
                    recording, segment.segment_id, start, stop, receiver_count
                )
                try:
                    values, decoded_samples = decode_ci16(raw, receiver_count)
                except Ci16DecodeError as exc:
                    raise AnalysisInputError(
                        f"segment {segment.segment_id} PSD window malformed: {exc}"
                    ) from exc
                if decoded_samples != self._config.psd_window_samples:
                    raise AnalysisInputError("PSD window decoded to an unexpected size")
                stride = receiver_count * 2
                for receiver_index, receiver_id in enumerate(receiver_ids):
                    offset = receiver_index * 2
                    complex_samples = [
                        complex(values[position], values[position + 1])
                        for position in range(offset, len(values), stride)
                    ]
                    summary = compact_psd(
                        complex_samples,
                        sample_rate_hz=segment.actual_sample_rate_hz,
                        noise_floor_epsilon=self._config.noise_floor_epsilon,
                    )
                    score = MethodScore(
                        method_id=PSD_METHOD_ID,
                        method_version=ALGORITHM_VERSION,
                        segment_id=segment.segment_id,
                        receiver_key=str(receiver_id),
                        window_start_sample=start,
                        window_stop_sample=stop,
                        score=summary.peak_to_median_ratio,
                        score_semantics="peak-psd-to-median-psd-ratio",
                    )
                    method_scores.append(score)
                    feature_id = FeatureId(
                        _derived_id(
                            "feature",
                            {
                                "run": run_token,
                                "method": PSD_METHOD_ID,
                                "segment": str(segment.segment_id),
                                "receiver": str(receiver_id),
                                "start": start,
                                "stop": stop,
                            },
                        )
                    )
                    observations.append(
                        FeatureObservation(
                            feature_id=feature_id,
                            recording_id=request.recording_id,
                            segment_id=segment.segment_id,
                            method_id=PSD_METHOD_ID,
                            method_version=ALGORITHM_VERSION,
                            window_start_sample=start,
                            window_stop_sample=stop,
                            segment_sample_count=segment.sample_count,
                            midpoint_utc_ns=_midpoint_utc_ns(segment, start, stop),
                            feature_kind="compact-psd-peak",
                            score=summary.peak_to_median_ratio,
                            score_semantics="peak-psd-to-median-psd-ratio",
                            receiver_chain_id=receiver_id,
                            frequency_hz=(
                                segment.actual_center_frequency_hz
                                + summary.frequency_offset_hz
                            ),
                            frequency_offset_hz=summary.frequency_offset_hz,
                            noise_estimate=summary.median_noise_power,
                            snr_db=summary.snr_db,
                            uncertainty=(
                                (
                                    "frequency_bin_width_hz",
                                    segment.actual_sample_rate_hz
                                    / self._config.psd_window_samples,
                                ),
                            ),
                            diagnostics=(
                                ("peak_bin", summary.peak_bin),
                                ("peak_power_counts_squared", summary.peak_power),
                                ("window_function", "rectangular-demeaned"),
                            ),
                        )
                    )

        provenance = Provenance(
            producer_name=self._execution.producer_name,
            producer_version=self._execution.producer_version,
            git_commit=self._execution.git_commit,
            environment_digest=self._execution.environment_digest,
            normalized_config_digest=request.config_ref.digest,
            input_digests=(recording_digest,),
            dependency_digests=(request.algorithm_ref.digest,)
            + tuple(ref.digest for ref in dependencies),
            started_utc_ns=self._execution.started_utc_ns,
            completed_utc_ns=self._execution.completed_utc_ns,
            host_class=self._execution.host_class,
        )
        return FeatureSetBundle(
            schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
            feature_set_id=feature_set_id,
            analysis_run_id=analysis_run_id,
            recording_id=request.recording_id,
            input_recording_identity_digest=recording_digest,
            provenance=provenance,
            observations=tuple(observations),
            method_scores=tuple(method_scores),
            warnings=tuple(sorted(set(warnings))),
            reason_codes=tuple(sorted(reason_codes)),
        )

    @staticmethod
    def _read_exact(
        recording: RecordingView,
        segment_id: SegmentId,
        start: int,
        stop: int,
        receiver_count: int,
    ) -> bytes:
        expected_bytes = (stop - start) * receiver_count * 4
        try:
            raw = recording.read_iq_bytes(segment_id, start, stop)
        except Exception as exc:
            raise AnalysisInputError(
                f"recording reader failed for {segment_id}[{start}:{stop}]: {exc}"
            ) from exc
        if not isinstance(raw, bytes):
            raise AnalysisInputError("recording reader must return immutable bytes")
        if len(raw) != expected_bytes:
            raise AnalysisInputError(
                f"recording reader returned {len(raw)} bytes; expected {expected_bytes}"
            )
        return raw
