"""Native Redux replay of the frozen historical Starlink pilot/QAM oracle."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_pilot_constellation import (
    StarlinkPilotConstellationEvidenceV0_1,
)
from leo_flow.contracts.starlink_retro_qam_canary import (
    RetroQamDualReceiverMetricsV0_1,
    RetroQamReceiverMetricsV0_1,
    StarlinkRetroQamCanaryReceiptV0_1,
)

from .api import AnalysisExecutionContext
from .starlink_acquisition import StarlinkAcquisitionConfigV0_3, StarlinkAcquisitionV0_3
from .starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
)
from .starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
    starlink_pilot_constellation_algorithm_ref_v0_1,
)
from .starlink_templates import qin_edge_pilot_template_pair_v0_1


@dataclass(frozen=True)
class RetroQamReceiverExpectationV0_1:
    receiver_index: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    complete_frame_count: int
    hard_symbol_accuracy: float
    rms_evm: float


@dataclass(frozen=True)
class RetroQamCombinedExpectationV0_1:
    hard_symbol_accuracy: float
    rms_evm: float
    soft_mean_confidence: float


@dataclass(frozen=True)
class RetroQamCanaryInputV0_1:
    corpus_id: str
    corpus_manifest_digest: Digest
    iq_object_digest: Digest
    selected_window_digest: Digest
    selected_window_start_sample: int
    sample_rate_hz: float
    samples: tuple[np.ndarray, np.ndarray]
    receiver_expectations: tuple[
        RetroQamReceiverExpectationV0_1, RetroQamReceiverExpectationV0_1
    ]
    combined_expectation: RetroQamCombinedExpectationV0_1

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("retro-QAM sample rate must be positive")
        if tuple(item.receiver_index for item in self.receiver_expectations) != (0, 1):
            raise ValueError("retro-QAM receiver expectations must be canonical")
        if len(self.samples[0]) != len(self.samples[1]) or not len(self.samples[0]):
            raise ValueError("retro-QAM receiver sample windows must match")


def analyze_starlink_retro_qam_canary_v0_1(
    request: RetroQamCanaryInputV0_1,
    execution: AnalysisExecutionContext,
) -> StarlinkRetroQamCanaryReceiptV0_1:
    """Search, demodulate, combine, and compare one immutable oracle window."""
    templates = qin_edge_pilot_template_pair_v0_1(
        request.sample_rate_hz, StarlinkEdge.LOWER
    )
    identity = Digest.sha256(request.corpus_id.encode("utf-8"))
    acquired: list[StarlinkPilotConstellationEvidenceV0_1] = []
    acquisitions = []
    receiver_metrics: list[RetroQamReceiverMetricsV0_1] = []
    config_digests: list[Digest] = []
    for expected, values in zip(
        request.receiver_expectations, request.samples, strict=True
    ):
        receiver_chain_id = ReceiverChainId(f"rx_retro_qam_{expected.receiver_index}")
        sample_values = [complex(item) for item in values]
        acquisition_config = StarlinkAcquisitionConfigV0_3(
            f"pluto-5d4d-rx{expected.receiver_index}"
        )
        acquisition = StarlinkAcquisitionV0_3(
            acquisition_config, execution
        ).analyze_receiver(
            sample_values,
            recording_id=RecordingId("rec_retro_qam_20260813"),
            recording_identity_digest=identity,
            segment_id=SegmentId("seg_retro_qam_68p7s"),
            receiver_chain_id=receiver_chain_id,
            templates=templates,
        )
        acquisitions.append(acquisition)
        winner = acquisition.winner
        suite = StarlinkDetectorSuiteV0_2(
            StarlinkDetectorSuiteConfigV0_2(
                (winner.refined_epoch_sample,),
                (winner.refined_cfo_hz,),
                (0.0,),
                maximum_probe_samples=len(values),
            ),
            execution,
        ).analyze_receiver(
            sample_values,
            recording_id=RecordingId("rec_retro_qam_20260813"),
            recording_identity_digest=identity,
            segment_id=SegmentId("seg_retro_qam_68p7s"),
            receiver_chain_id=receiver_chain_id,
            templates=templates,
        )
        qam = StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=len(values)),
            execution,
        ).analyze(sample_values, suite)
        acquired.append(qam)
        config_digests.append(acquisition.config_ref.digest)
        receiver_metrics.append(
            RetroQamReceiverMetricsV0_1(
                expected.receiver_index,
                winner.refined_epoch_sample,
                winner.refined_cfo_hz,
                winner.acquire_score,
                winner.verify_score,
                winner.conditioned_control_score,
                winner.verify_minus_control_margin,
                qam.complete_frame_count,
                qam.hard_symbol_accuracy,
                qam.rms_evm,
                qam.soft_mean_confidence,
                qam.soft_mean_expected_probability,
                qam.soft_mean_entropy_bits,
                qam.soft_noise_variance,
                qam.model_snr_db,
            )
        )

    combined = _combine_equalized_receivers((acquired[0], acquired[1]))
    matches = all(
        _receiver_matches(actual, expected)
        for actual, expected in zip(
            receiver_metrics, request.receiver_expectations, strict=True
        )
    ) and _combined_matches(combined, request.combined_expectation)
    return StarlinkRetroQamCanaryReceiptV0_1(
        SchemaRef(StarlinkRetroQamCanaryReceiptV0_1.SCHEMA_ID),
        request.corpus_id,
        request.corpus_manifest_digest,
        request.iq_object_digest,
        request.selected_window_digest,
        request.selected_window_start_sample,
        len(request.samples[0]),
        acquisitions[0].algorithm_ref.digest,
        (config_digests[0], config_digests[1]),
        starlink_pilot_constellation_algorithm_ref_v0_1().digest,
        (receiver_metrics[0], receiver_metrics[1]),
        combined,
        matches,
        True,
        None,
        execution.producer_name,
        execution.producer_version,
        execution.git_commit,
        execution.completed_utc_ns,
        (
            "known-published-pilot-regression",
            "candidate-evidence-not-calibrated-detection",
            "leo-tracker-oracle-not-runtime-dependency",
            "whole-input-sha256-verified-before-analysis",
        ),
    )


def _combine_equalized_receivers(
    evidence: tuple[
        StarlinkPilotConstellationEvidenceV0_1,
        StarlinkPilotConstellationEvidenceV0_1,
    ],
) -> RetroQamDualReceiverMetricsV0_1:
    values = []
    expected_states = []
    noise = []
    for raw in evidence:
        points = raw.points
        values.append(np.asarray([complex(item.i, item.q) for item in points]))
        expected_states.append(np.asarray([item.expected_state for item in points]))
        noise.append(float(raw.soft_noise_variance))
    if not np.array_equal(expected_states[0], expected_states[1]):
        raise ValueError("receiver QAM points do not share the known pilot states")
    inverse = 1 / np.maximum(np.asarray(noise, dtype=float), 1e-6)
    weights = inverse / np.sum(inverse)
    equalized = weights[0] * values[0] + weights[1] * values[1]
    expected = np.exp(0.5j * np.pi * (expected_states[0].astype(float) + 0.5))
    constellation = np.exp(0.5j * np.pi * (np.arange(4, dtype=float) + 0.5))
    distances = np.abs(equalized[:, None] - constellation[None, :]) ** 2
    hard = np.argmin(distances, axis=1)
    error = equalized - expected
    noise_variance = max(float(np.mean(np.abs(error) ** 2)), 1e-6)
    logits = -distances / noise_variance
    logits -= np.max(logits, axis=1, keepdims=True)
    likelihood = np.exp(logits)
    probabilities = likelihood / np.sum(likelihood, axis=1, keepdims=True)
    expected_probability = probabilities[
        np.arange(len(expected_states[0])), expected_states[0]
    ]
    entropy = -np.sum(probabilities * np.log2(np.maximum(probabilities, 1e-12)), axis=1)
    return RetroQamDualReceiverMetricsV0_1(
        "inverse-noise-equalized-dual-rx",
        len(equalized),
        (float(weights[0]), float(weights[1])),
        float(np.mean(hard == expected_states[0])),
        float(math.sqrt(noise_variance)),
        float(np.median(np.abs(equalized))),
        float(np.mean(np.max(probabilities, axis=1))),
        float(np.mean(expected_probability)),
        float(np.mean(entropy)),
        noise_variance,
    )


def _receiver_matches(
    actual: RetroQamReceiverMetricsV0_1,
    expected: RetroQamReceiverExpectationV0_1,
) -> bool:
    return (
        actual.winning_epoch_sample == expected.winning_epoch_sample
        and abs(actual.winning_cfo_hz - expected.winning_cfo_hz) <= 35
        and actual.verify_minus_control_margin > 0.3
        and actual.complete_frame_count == expected.complete_frame_count
        and abs(actual.hard_symbol_accuracy - expected.hard_symbol_accuracy)
        <= 1 / 2_400
        and abs(actual.rms_evm - expected.rms_evm) <= 1e-4
    )


def _combined_matches(
    actual: RetroQamDualReceiverMetricsV0_1,
    expected: RetroQamCombinedExpectationV0_1,
) -> bool:
    return (
        abs(actual.hard_symbol_accuracy - expected.hard_symbol_accuracy) <= 2 / 2_400
        and abs(actual.rms_evm - expected.rms_evm) <= 5e-4
        and abs(actual.soft_mean_confidence - expected.soft_mean_confidence) <= 1e-4
    )
