"""Native bounded demodulation of the published Starlink edge pilots."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from leo_flow.contracts._validation import require_finite
from leo_flow.contracts.core import ArtifactRef, Provenance, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_detector_suite import (
    StarlinkDetectorMethod,
    StarlinkDetectorSuiteBundleV0_2,
)
from leo_flow.contracts.starlink_pilot_constellation import (
    StarlinkPilotConstellationEvidenceV0_1,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)

from .api import AnalysisExecutionContext
from .starlink import FRAME_RATE_HZ
from .starlink_templates import (
    CYCLIC_PREFIX_DURATION_S,
    OFDM_SYMBOL_DURATION_S,
    SUBCARRIER_SPACING_HZ,
    qin_edge_pilot_artifacts_v0_1,
    qin_edge_pilot_indices_v1,
    qin_edge_pilot_states_v1,
)

ALGORITHM_ID = "starlink-published-edge-pilot-constellation"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.starlink-pilot-constellation-config"
MINIMUM_SAMPLE_RATE_HZ = 8 * SUBCARRIER_SPACING_HZ


@dataclass(frozen=True)
class StarlinkPilotConstellationConfigV0_1:
    maximum_probe_samples: int = 50_000_000
    maximum_complete_frames: int = 20_000
    residual_cfo_limit_hz: float = 2_000.0
    soft_noise_variance_floor: float = 1e-6

    def __post_init__(self) -> None:
        for name in ("maximum_probe_samples", "maximum_complete_frames"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("residual_cfo_limit_hz", "soft_noise_variance_floor"):
            require_finite(getattr(self, name), name)
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


def starlink_pilot_constellation_algorithm_ref_v0_1() -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "coefficient_estimator": "per-symbol-complex-least-squares-eight-pilots",
                "frame_alignment": "known-code-match-phase",
                "stack": "clipped-quality-weighted",
                "equalizer": "opposite-symbol-parity-cross-fit",
                "constellation": "rotated-qpsk-known-published-pilot",
                "working_set": "streaming-five-pass-o(frames-plus-2400)",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm"),
    )


def starlink_pilot_constellation_config_ref_v0_1(
    config: StarlinkPilotConstellationConfigV0_1,
) -> ArtifactRef:
    return ArtifactRef(
        "starlink-pilot-constellation-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID),
    )


class StarlinkPilotConstellationAnalyzerV0_1:
    """Demodulate one suite stream without retaining per-frame symbol tensors."""

    def __init__(
        self,
        config: StarlinkPilotConstellationConfigV0_1,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._execution = execution

    def analyze(
        self,
        samples: Sequence[complex],
        suite: StarlinkDetectorSuiteBundleV0_2,
    ) -> StarlinkPilotConstellationEvidenceV0_1:
        sample_count = len(samples)
        if sample_count != suite.probe_sample_count:
            raise ValueError("sample count does not match source detector suite")
        if sample_count <= 0 or sample_count > self._config.maximum_probe_samples:
            raise ValueError("probe sample count lies outside the configured bound")
        if suite.sample_rate_hz < MINIMUM_SAMPLE_RATE_HZ:
            raise ValueError("sample rate is too low for all eight edge pilots")
        acquire = next(
            (
                item
                for item in suite.methods
                if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
            ),
            None,
        )
        if acquire is None:
            raise ValueError("source suite has no full-frame acquire evidence")
        exact_artifact, _ = qin_edge_pilot_artifacts_v0_1(
            suite.sample_rate_hz, suite.edge
        )
        if acquire.exact_template_ref != exact_artifact.template_ref:
            raise ValueError(
                "source acquire winner does not bind the exact Qin template"
            )
        if acquire.winning_epoch_sample >= sample_count:
            raise ValueError("acquire winner epoch lies outside the input")

        rate = suite.sample_rate_hz
        frame_starts = _complete_frame_starts(
            sample_count, rate, acquire.winning_epoch_sample
        )
        if not frame_starts:
            raise ValueError("window contains no complete Starlink frame")
        if len(frame_starts) > self._config.maximum_complete_frames:
            raise ValueError("complete-frame support exceeds the configured bound")
        indexes = qin_edge_pilot_indices_v1(suite.edge)
        frequencies = _edge_frequencies_hz(indexes)
        expected_states = np.asarray(qin_edge_pilot_states_v1(suite.edge), np.int8)
        ideal = np.exp(0.5j * np.pi * (expected_states.astype(float) + 0.5))
        base_cfo_hz = acquire.winning_coarse_cfo_hz + acquire.winning_residual_cfo_hz
        demodulator = _StreamingDemodulator(samples, rate, frequencies, base_cfo_hz)

        # Two streaming passes reproduce the oracle residual-CFO estimate without
        # retaining a frame x 300 x 8 tensor (about 288 MB for a 20 s dwell).
        initial_channel = np.zeros(8, np.complex128)
        for start in frame_starts:
            pilots = demodulator.frame(start)
            phase = np.angle(np.sum(pilots * np.conj(ideal)))
            initial_channel += np.sum(
                pilots * np.exp(-1j * phase) * np.conj(ideal), axis=0
            )
        initial_channel /= len(frame_starts) * 300
        slopes = []
        pilot_times_s = (np.arange(300, dtype=float) + 2.5) * OFDM_SYMBOL_DURATION_S
        centered_times_s = pilot_times_s - np.mean(pilot_times_s)
        for start in frame_starts:
            pilots = demodulator.frame(start)
            symbol_match = np.sum(
                pilots * np.conj(ideal) * np.conj(initial_channel)[None, :], axis=1
            )
            phase = np.unwrap(np.angle(symbol_match))
            weight = np.abs(symbol_match)
            usable = weight > np.median(weight) * 0.25
            if np.count_nonzero(usable) >= 20:
                slope = np.polyfit(
                    centered_times_s[usable],
                    phase[usable],
                    1,
                    w=np.sqrt(weight[usable]),
                )[0]
                slopes.append(float(slope / (2 * np.pi)))
        residual_cfo_hz = float(
            np.clip(
                np.median(slopes) if slopes else 0.0,
                -self._config.residual_cfo_limit_hz,
                self._config.residual_cfo_limit_hz,
            )
        )
        residual_rotation = np.exp(-2j * np.pi * residual_cfo_hz * centered_times_s)[
            :, None
        ]

        qualities = np.empty(len(frame_starts), dtype=float)
        phases = np.empty(len(frame_starts), dtype=float)
        for frame_index, start in enumerate(frame_starts):
            pilots = demodulator.frame(start) * residual_rotation
            match = np.sum(pilots * np.conj(ideal))
            phases[frame_index] = np.angle(match)
            energy = float(np.sum(np.abs(pilots) ** 2))
            qualities[frame_index] = abs(match) ** 2 / max(energy, 1e-20)
        positive = qualities[qualities > 0]
        if positive.size:
            qualities = np.minimum(qualities, 4 * np.median(positive))
        quality_sum = float(np.sum(qualities))
        weights = (
            qualities / quality_sum
            if quality_sum > 0
            else np.full(len(frame_starts), 1 / len(frame_starts))
        )
        stacked = np.zeros((300, 8), np.complex128)
        for frame_index, start in enumerate(frame_starts):
            pilots = demodulator.frame(start) * residual_rotation
            stacked += pilots * np.exp(-1j * phases[frame_index]) * weights[frame_index]

        equalized = np.empty_like(stacked)
        symbol_indexes = np.arange(300)
        for parity in range(2):
            training = symbol_indexes % 2 != parity
            testing = ~training
            channel = np.mean(stacked[training] * np.conj(ideal[training]), axis=0)
            equalized[testing] = stacked[testing] / np.where(
                np.abs(channel) > 1e-20, channel, 1 + 0j
            )
        full_channel = np.mean(stacked * np.conj(ideal), axis=0)

        signal_energy = 0.0
        residual_energy = 0.0
        for frame_index, start in enumerate(frame_starts):
            pilots = demodulator.frame(start) * residual_rotation
            modeled = np.exp(1j * phases[frame_index]) * full_channel[None, :] * ideal
            signal_energy += float(np.sum(np.abs(modeled) ** 2))
            residual_energy += float(np.sum(np.abs(pilots - modeled) ** 2))

        hard_states, probabilities, correct, errors = _decisions(
            equalized, expected_states, self._config.soft_noise_variance_floor
        )
        noise_variance = max(
            float(np.mean(np.abs(errors) ** 2)),
            self._config.soft_noise_variance_floor,
        )
        confidence = np.max(probabilities, axis=-1)
        expected_probability = np.take_along_axis(
            probabilities, expected_states[..., None], axis=-1
        )[..., 0]
        entropy = -np.sum(
            probabilities * np.log2(np.maximum(probabilities, 1e-12)), axis=-1
        )
        points = tuple(
            StarlinkPilotConstellationPointV0_1(
                symbol + 2,
                indexes[subcarrier],
                int(expected_states[symbol, subcarrier]),
                int(hard_states[symbol, subcarrier]),
                float(equalized[symbol, subcarrier].real),
                float(equalized[symbol, subcarrier].imag),
                bool(correct[symbol, subcarrier]),
                float(confidence[symbol, subcarrier]),
                float(expected_probability[symbol, subcarrier]),
                float(entropy[symbol, subcarrier]),
            )
            for symbol in range(300)
            for subcarrier in range(8)
        )
        subcarriers = tuple(
            StarlinkPilotSubcarrierSummaryV0_1(
                indexes[column],
                float(frequencies[column]),
                float(np.mean(correct[:, column])),
                float(np.sqrt(np.mean(np.abs(errors[:, column]) ** 2))),
                float(abs(full_channel[column])),
                float(np.rad2deg(np.angle(full_channel[column]))),
            )
            for column in range(8)
        )
        config_ref = starlink_pilot_constellation_config_ref_v0_1(self._config)
        algorithm_ref = starlink_pilot_constellation_algorithm_ref_v0_1()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            config_ref.digest,
            (suite.recording_identity_digest, suite.digest),
            (algorithm_ref.digest, acquire.exact_template_ref.digest),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        identity = canonical_digest(
            {
                "source_suite_digest": str(suite.digest),
                "acquire_search_identity_digest": str(acquire.search_identity_digest),
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(config_ref.digest),
            }
        )
        effective_frames = float(1 / np.sum(weights**2))
        return StarlinkPilotConstellationEvidenceV0_1(
            SchemaRef(StarlinkPilotConstellationEvidenceV0_1.SCHEMA_ID),
            f"slqam_{identity.value[:32]}",
            suite.recording_id,
            suite.recording_identity_digest,
            suite.segment_id,
            suite.receiver_chain_id,
            suite.edge,
            rate,
            sample_count,
            suite.analysis_id,
            suite.digest,
            suite.suite_identity_digest,
            StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
            acquire.search_identity_digest,
            acquire.algorithm_ref,
            acquire.config_ref,
            acquire.exact_template_ref,
            acquire.winning_epoch_sample,
            acquire.winning_coarse_cfo_hz,
            acquire.winning_residual_cfo_hz,
            residual_cfo_hz,
            len(frame_starts),
            effective_frames,
            float(10 * np.log10(effective_frames)),
            2_400,
            float(np.mean(correct)),
            0.25,
            float(np.sqrt(np.mean(np.abs(errors) ** 2))),
            float(np.median(np.abs(equalized))),
            float(np.mean(confidence)),
            float(np.mean(expected_probability)),
            float(np.mean(entropy)),
            noise_variance,
            float(
                10 * np.log10(max(signal_energy, 1e-30) / max(residual_energy, 1e-30))
            ),
            subcarriers,
            points,
            "quality-weighted-stack-all-300x8-cross-fitted",
            provenance,
            True,
            True,
            False,
            (
                "candidate-evidence-not-calibrated-detection",
                "published-edge-pilot-not-user-payload",
                "conditioned-on-full-frame-acquire-winner",
                "opposite-symbol-parity-cross-fitted-channel",
            ),
        )


class _StreamingDemodulator:
    def __init__(
        self,
        samples: Sequence[complex],
        sample_rate_hz: float,
        frequencies_hz: np.ndarray,
        carrier_offset_hz: float,
    ) -> None:
        self._samples = samples
        self._rate = sample_rate_hz
        self._frequencies = frequencies_hz
        self._cfo = carrier_offset_hz
        self._designs: dict[tuple[int, int], np.ndarray] = {}

    def frame(self, frame_start: int) -> np.ndarray:
        result = np.empty((300, 8), dtype=np.complex128)
        for row, symbol in enumerate(range(2, 302)):
            local_start = round(symbol * self._rate * OFDM_SYMBOL_DURATION_S)
            local_stop = round((symbol + 1) * self._rate * OFDM_SYMBOL_DURATION_S)
            key = (local_start, local_stop)
            solve = self._designs.get(key)
            if solve is None:
                local_indexes = np.arange(local_start, local_stop)
                local_time_s = (
                    local_indexes / self._rate
                    - symbol * OFDM_SYMBOL_DURATION_S
                    - CYCLIC_PREFIX_DURATION_S
                )
                design = np.exp(
                    2j * np.pi * local_time_s[:, None] * self._frequencies[None, :]
                ) / math.sqrt(8)
                solve = np.linalg.pinv(design)
                self._designs[key] = solve
            start = frame_start + local_start
            stop = frame_start + local_stop
            values = np.asarray(self._samples[start:stop], dtype=np.complex128)
            absolute_indexes = np.arange(start, stop)
            values *= np.exp(-2j * np.pi * self._cfo * absolute_indexes / self._rate)
            result[row] = solve @ values
        return result


def _complete_frame_starts(
    sample_count: int, sample_rate_hz: float, epoch_sample: int
) -> tuple[int, ...]:
    frame_content_samples = round(302 * sample_rate_hz * OFDM_SYMBOL_DURATION_S)
    result: list[int] = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * sample_rate_hz / FRAME_RATE_HZ)
        if start + frame_content_samples > sample_count:
            return tuple(result)
        result.append(start)
        frame += 1


def _edge_frequencies_hz(indexes: tuple[int, ...]) -> np.ndarray:
    absolute = np.asarray(
        [
            (index if index < 512 else index - 1024) * SUBCARRIER_SPACING_HZ
            for index in indexes
        ],
        dtype=float,
    )
    return np.asarray(absolute - np.mean(absolute), dtype=float)


def _decisions(
    equalized: np.ndarray,
    expected_states: np.ndarray,
    noise_floor: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    constellation = np.exp(0.5j * np.pi * (np.arange(4, dtype=float) + 0.5))
    ideal = constellation[expected_states]
    error = equalized - ideal
    noise_variance = max(float(np.mean(np.abs(error) ** 2)), noise_floor)
    distance = np.abs(equalized[..., None] - constellation) ** 2
    logits = -distance / noise_variance
    logits -= np.max(logits, axis=-1, keepdims=True)
    likelihood = np.exp(logits)
    probabilities = likelihood / np.sum(likelihood, axis=-1, keepdims=True)
    hard = np.argmin(distance, axis=-1)
    return hard, probabilities, hard == expected_states, error
