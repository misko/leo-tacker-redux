"""Versioned, one-recording detector suite with aligned score windows."""

from __future__ import annotations

import math
from dataclasses import dataclass

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverPairId,
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

from .api import (
    AnalysisConfigurationError,
    AnalysisExecutionContext,
    AnalysisInputError,
)
from .detectors import (
    coarse_energy,
    paired_common_mode,
    periodic_coherence,
    robust_pair_score,
)
from .quality import Ci16DecodeError, decode_ci16

ALGORITHM_ID = "independent-detector-suite"
ALGORITHM_VERSION = "0.1.0"
PERIODIC_METHOD_ID = "periodic-coherence"
PAIRED_METHOD_ID = "paired-common-mode"
ENERGY_METHOD_ID = "coarse-energy"
CONFIG_SCHEMA_ID = "org.leo-flow.independent-detector-config"


@dataclass(frozen=True)
class DetectorSuiteConfig:
    window_samples: int = 1024
    stride_samples: int = 1024
    periodic_lag_samples: int = 64
    max_pair_delay_samples: int = 4
    clip_threshold_abs: int = 32760
    refuse_clipping: bool = True
    noise_floor_epsilon: float = 1e-12

    def __post_init__(self) -> None:
        if (
            isinstance(self.window_samples, bool)
            or not isinstance(self.window_samples, int)
            or self.window_samples < 8
            or self.window_samples & (self.window_samples - 1)
        ):
            raise ValueError("window_samples must be a power of two >= 8")
        for name in ("stride_samples", "periodic_lag_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.periodic_lag_samples >= self.window_samples:
            raise ValueError("periodic_lag_samples must lie inside a window")
        if (
            isinstance(self.max_pair_delay_samples, bool)
            or not isinstance(self.max_pair_delay_samples, int)
            or not 0 <= self.max_pair_delay_samples < self.window_samples
        ):
            raise ValueError("max_pair_delay_samples must lie inside a window")
        if (
            isinstance(self.clip_threshold_abs, bool)
            or not isinstance(self.clip_threshold_abs, int)
            or not 1 <= self.clip_threshold_abs <= 32768
        ):
            raise ValueError("clip_threshold_abs must lie in [1, 32768]")
        if not isinstance(self.refuse_clipping, bool):
            raise TypeError("refuse_clipping must be boolean")
        if (
            isinstance(self.noise_floor_epsilon, bool)
            or not isinstance(self.noise_floor_epsilon, (int, float))
            or not math.isfinite(self.noise_floor_epsilon)
            or self.noise_floor_epsilon <= 0
        ):
            raise ValueError("noise_floor_epsilon must be positive and finite")


def detector_suite_algorithm_ref() -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "input": "paired-ci16-le",
                "methods": [ENERGY_METHOD_ID, PAIRED_METHOD_ID, PERIODIC_METHOD_ID],
                "decision": "external-calibrated-threshold-rule",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm"),
    )


def detector_suite_config_ref(config: DetectorSuiteConfig) -> ArtifactRef:
    return ArtifactRef(
        "independent-detector-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


def _starts(count: int, size: int, stride: int) -> tuple[int, ...]:
    if count < size:
        return ()
    starts = list(range(0, count - size + 1, stride))
    last = count - size
    if starts[-1] != last:
        starts.append(last)
    return tuple(starts)


def _id(prefix: str, value: object) -> str:
    return f"{prefix}_{canonical_digest(value).value[:32]}"


class IndependentDetectorSuite:
    """Extract three hypothesis-neutral methods from one recording at a time."""

    def __init__(
        self, config: DetectorSuiteConfig, execution: AnalysisExecutionContext
    ) -> None:
        self._config = config
        self._execution = execution

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
        if request.algorithm_ref != detector_suite_algorithm_ref():
            raise AnalysisConfigurationError(
                "request algorithm_ref does not identify this analyzer"
            )
        if request.config_ref != detector_suite_config_ref(self._config):
            raise AnalysisConfigurationError(
                "request config_ref does not match analyzer config"
            )
        dependency_ids = [ref.artifact_id for ref in request.dependency_refs]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise AnalysisConfigurationError("dependency_refs contain duplicates")
        dependencies = tuple(
            sorted(
                request.dependency_refs,
                key=lambda ref: (ref.artifact_id, str(ref.digest)),
            )
        )
        recording_digest = request.recording_object_ref.identity_digest()
        run_identity = {
            "recording": str(recording_digest),
            "algorithm": str(request.algorithm_ref.digest),
            "config": str(request.config_ref.digest),
            "dependencies": [str(ref.digest) for ref in dependencies],
            "environment": str(self._execution.environment_digest),
            "git_commit": self._execution.git_commit,
        }
        token = canonical_digest(run_identity).value
        observations: list[FeatureObservation] = []
        scores: list[MethodScore] = []
        warnings: set[str] = set()
        reasons: set[str] = set()

        for segment in manifest.segments:
            receivers = segment.requested.receiver_chain_ids
            if len(receivers) != 2:
                warnings.add(f"{segment.segment_id}:requires-exactly-two-receivers")
                reasons.add("unsupported-receiver-count")
                continue
            pair_id = ReceiverPairId(f"rxpair_{receivers[0]}_{receivers[1]}")
            receiver_key = str(pair_id)
            starts = _starts(
                segment.sample_count,
                self._config.window_samples,
                self._config.stride_samples,
            )
            if not starts:
                warnings.add(f"{segment.segment_id}:segment-too-short")
                reasons.add("detectors-skipped-short-segment")
                continue
            for start in starts:
                stop = start + self._config.window_samples
                raw = self._read_exact(recording, segment.segment_id, start, stop)
                try:
                    values, decoded = decode_ci16(raw, 2)
                except Ci16DecodeError as exc:
                    raise AnalysisInputError(
                        f"segment {segment.segment_id} has malformed paired CI16: {exc}"
                    ) from exc
                if decoded != self._config.window_samples:
                    raise AnalysisInputError(
                        "detector window decoded to an unexpected size"
                    )
                channels = (
                    [
                        complex(values[index], values[index + 1])
                        for index in range(0, len(values), 4)
                    ],
                    [
                        complex(values[index], values[index + 1])
                        for index in range(2, len(values), 4)
                    ],
                )
                if all(value == 0j for channel in channels for value in channel):
                    warnings.add(f"{segment.segment_id}:{start}:{stop}:zero-energy")
                    reasons.add("detector-refused-zero-energy")
                    continue
                clipped = sum(
                    abs(component) >= self._config.clip_threshold_abs
                    for component in values
                )
                if clipped and self._config.refuse_clipping:
                    warnings.add(f"{segment.segment_id}:{start}:{stop}:clipping")
                    reasons.add("detector-refused-clipping")
                    continue

                periodic = [
                    periodic_coherence(channel, self._config.periodic_lag_samples)
                    for channel in channels
                ]
                energies = [
                    coarse_energy(
                        channel,
                        sample_rate_hz=segment.actual_sample_rate_hz,
                        epsilon=self._config.noise_floor_epsilon,
                    )
                    for channel in channels
                ]
                paired = paired_common_mode(
                    channels[0],
                    channels[1],
                    max_delay_samples=self._config.max_pair_delay_samples,
                )
                midpoint = UtcNs(
                    segment.start_utc_ns
                    + round(((start + stop) / 2) * 1e9 / segment.actual_sample_rate_hz)
                )
                common = {
                    "run": token,
                    "segment": str(segment.segment_id),
                    "start": start,
                    "stop": stop,
                }
                for receiver, periodic_evidence in zip(
                    receivers, periodic, strict=True
                ):
                    observations.append(
                        FeatureObservation(
                            feature_id=FeatureId(
                                _id(
                                    "feature",
                                    common
                                    | {
                                        "method": PERIODIC_METHOD_ID,
                                        "receiver": str(receiver),
                                    },
                                )
                            ),
                            recording_id=request.recording_id,
                            segment_id=segment.segment_id,
                            method_id=PERIODIC_METHOD_ID,
                            method_version=ALGORITHM_VERSION,
                            window_start_sample=start,
                            window_stop_sample=stop,
                            segment_sample_count=segment.sample_count,
                            midpoint_utc_ns=midpoint,
                            feature_kind="complex-lag-coherence",
                            score=periodic_evidence.score,
                            score_semantics="normalized-complex-autocorrelation-magnitude",
                            receiver_chain_id=receiver,
                            uncertainty=(
                                ("lag_resolution_samples", 1),
                                ("calibration_status", "uncalibrated"),
                            ),
                            quality_flags=(("clipping_detected",) if clipped else ()),
                            diagnostics=(
                                ("lag_samples", periodic_evidence.lag_samples),
                                (
                                    "numerator_magnitude",
                                    periodic_evidence.numerator_magnitude,
                                ),
                                ("normalization", periodic_evidence.normalization),
                            ),
                        )
                    )
                for receiver, energy_evidence in zip(receivers, energies, strict=True):
                    observations.append(
                        FeatureObservation(
                            feature_id=FeatureId(
                                _id(
                                    "feature",
                                    common
                                    | {
                                        "method": ENERGY_METHOD_ID,
                                        "receiver": str(receiver),
                                    },
                                )
                            ),
                            recording_id=request.recording_id,
                            segment_id=segment.segment_id,
                            method_id=ENERGY_METHOD_ID,
                            method_version=ALGORITHM_VERSION,
                            window_start_sample=start,
                            window_stop_sample=stop,
                            segment_sample_count=segment.sample_count,
                            midpoint_utc_ns=midpoint,
                            feature_kind="coarse-fft-energy-candidate",
                            score=energy_evidence.score,
                            score_semantics="peak-fft-bin-to-median-background-power-ratio",
                            receiver_chain_id=receiver,
                            frequency_hz=segment.actual_center_frequency_hz
                            + energy_evidence.frequency_offset_hz,
                            frequency_offset_hz=energy_evidence.frequency_offset_hz,
                            noise_estimate=energy_evidence.median_noise_power,
                            uncertainty=(
                                (
                                    "frequency_bin_width_hz",
                                    segment.actual_sample_rate_hz
                                    / self._config.window_samples,
                                ),
                                ("calibration_status", "uncalibrated"),
                            ),
                            quality_flags=(("clipping_detected",) if clipped else ()),
                            diagnostics=(
                                ("peak_bin", energy_evidence.peak_bin),
                                (
                                    "peak_power_counts_squared",
                                    energy_evidence.peak_power,
                                ),
                                ("window_function", "rectangular-demeaned"),
                            ),
                        )
                    )
                observations.append(
                    FeatureObservation(
                        feature_id=FeatureId(
                            _id(
                                "feature",
                                common
                                | {"method": PAIRED_METHOD_ID, "pair": receiver_key},
                            )
                        ),
                        recording_id=request.recording_id,
                        segment_id=segment.segment_id,
                        method_id=PAIRED_METHOD_ID,
                        method_version=ALGORITHM_VERSION,
                        window_start_sample=start,
                        window_stop_sample=stop,
                        segment_sample_count=segment.sample_count,
                        midpoint_utc_ns=midpoint,
                        feature_kind="paired-common-mode-coherence",
                        score=paired.score,
                        score_semantics="normalized-cross-channel-coherence-magnitude",
                        receiver_pair_id=pair_id,
                        uncertainty=(
                            ("delay_resolution_samples", 1),
                            ("calibration_status", "uncalibrated"),
                        ),
                        quality_flags=(
                            ("possible-conjugation",)
                            if paired.conjugate_score > paired.score
                            else ()
                        ),
                        diagnostics=(
                            ("delay_samples", paired.delay_samples),
                            ("relative_phase_rad", paired.relative_phase_rad),
                            ("gain_ratio", paired.gain_ratio),
                            (
                                "differential_power_fraction",
                                paired.differential_power_fraction,
                            ),
                            ("conjugate_score", paired.conjugate_score),
                        ),
                    )
                )
                method_values = (
                    (
                        PERIODIC_METHOD_ID,
                        robust_pair_score([item.score for item in periodic]),
                        "minimum-paired-normalized-autocorrelation",
                    ),
                    (
                        ENERGY_METHOD_ID,
                        robust_pair_score([item.score for item in energies]),
                        "minimum-paired-peak-to-median-power-ratio",
                    ),
                    (
                        PAIRED_METHOD_ID,
                        paired.score,
                        "normalized-cross-channel-coherence-magnitude",
                    ),
                )
                scores.extend(
                    MethodScore(
                        method,
                        ALGORITHM_VERSION,
                        segment.segment_id,
                        receiver_key,
                        start,
                        stop,
                        value,
                        semantics,
                    )
                    for method, value, semantics in method_values
                )

        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.config_ref.digest,
            (recording_digest,),
            (request.algorithm_ref.digest,) + tuple(ref.digest for ref in dependencies),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        return FeatureSetBundle(
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
            FeatureSetId(f"fset_{token[32:64]}"),
            AnalysisRunId(f"arun_{token[:32]}"),
            request.recording_id,
            recording_digest,
            provenance,
            tuple(observations),
            tuple(scores),
            warnings=tuple(sorted(warnings)),
            reason_codes=tuple(sorted(reasons)),
        )

    @staticmethod
    def _read_exact(
        recording: RecordingView, segment_id: SegmentId, start: int, stop: int
    ) -> bytes:
        try:
            raw = recording.read_iq_bytes(segment_id, start, stop)
        except Exception as exc:
            raise AnalysisInputError(
                f"recording reader failed for {segment_id}[{start}:{stop}]: {exc}"
            ) from exc
        expected = (stop - start) * 8
        if not isinstance(raw, bytes):
            raise AnalysisInputError("recording reader must return immutable bytes")
        if len(raw) != expected:
            raise AnalysisInputError(
                f"recording reader returned {len(raw)} bytes; expected {expected}"
            )
        return raw
