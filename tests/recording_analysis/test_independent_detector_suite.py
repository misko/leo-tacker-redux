from __future__ import annotations

import cmath
import math
import struct
from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.dataset import method_firing_association
from leo_flow.analysis.recording import (
    DetectorSuiteConfig,
    IndependentDetectorSuite,
    ThresholdRule,
    apply_threshold_rule,
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
)
from leo_flow.analysis.recording.detectors import (
    coarse_energy,
    paired_common_mode,
    periodic_coherence,
)
from leo_flow.contracts.core import Digest, SchemaRef, SegmentId
from leo_flow.contracts.features import (
    FeatureSetBundle,
    MethodScore,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.storage.ports import RecordingView

from .fakes import SegmentFixture, execution_context, make_view


def as_view(value: object) -> RecordingView:
    return cast(RecordingView, value)


def paired_bytes(first: list[complex], second: list[complex]) -> bytes:
    assert len(first) == len(second)
    return b"".join(
        struct.pack("<hhhh", round(a.real), round(a.imag), round(b.real), round(b.imag))
        for a, b in zip(first, second, strict=True)
    )


def periodic_tone(count: int, period: int = 8, amplitude: int = 1000) -> list[complex]:
    table = tuple(
        complex(
            round(amplitude * math.cos(2 * math.pi * n / period)),
            round(amplitude * math.sin(2 * math.pi * n / period)),
        )
        for n in range(period)
    )
    return [table[n % period] for n in range(count)]


def integer_noise(count: int, seed: int) -> list[complex]:
    state = seed
    result: list[complex] = []
    for _ in range(count):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        real = state % 2001 - 1000
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        imag = state % 2001 - 1000
        result.append(complex(real, imag))
    return result


def request(
    recording_ref: RecordingObjectRef, config: DetectorSuiteConfig
) -> RecordingAnalysisRequest:
    return RecordingAnalysisRequest(
        schema=SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording_id=recording_ref.recording_id,
        recording_object_ref=recording_ref,
        algorithm_ref=detector_suite_algorithm_ref(),
        config_ref=detector_suite_config_ref(config),
        dependency_refs=(),
        requested_output_schema=SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )


def test_kernels_match_independent_direct_definitions() -> None:
    values = periodic_tone(64)
    periodic = periodic_coherence(values, 8)
    numerator = sum(
        a.conjugate() * b for a, b in zip(values[:-8], values[8:], strict=True)
    )
    denominator = math.sqrt(
        sum(abs(a) ** 2 for a in values[:-8]) * sum(abs(b) ** 2 for b in values[8:])
    )
    assert periodic.score == pytest.approx(abs(numerator) / denominator, abs=1e-15)
    assert periodic.score == pytest.approx(1.0, abs=1e-15)

    energy = coarse_energy(values, sample_rate_hz=64_000, epsilon=1e-12)
    direct = [
        abs(
            sum(
                value * cmath.exp(-2j * math.pi * k * n / 64)
                for n, value in enumerate(values)
            )
        )
        ** 2
        / 64**2
        for k in range(64)
    ]
    assert energy.peak_bin == max(range(64), key=lambda index: (direct[index], -index))
    assert energy.frequency_offset_hz == 8_000
    assert energy.peak_power == pytest.approx(direct[energy.peak_bin], rel=1e-12)


def test_pair_kernel_recovers_delay_phase_gain_and_flags_conjugation() -> None:
    first = integer_noise(128, 7)
    second = [0j, 0j] + [2j * value for value in first[:-2]]
    evidence = paired_common_mode(first, second, max_delay_samples=4)
    assert evidence.delay_samples == 2
    assert evidence.score == pytest.approx(1.0, abs=1e-15)
    assert evidence.gain_ratio == pytest.approx(2.0, abs=1e-15)
    assert evidence.relative_phase_rad == pytest.approx(math.pi / 2, abs=1e-15)
    assert evidence.differential_power_fraction == pytest.approx(0.0, abs=1e-15)

    conjugated = [value.conjugate() for value in first]
    conjugate_evidence = paired_common_mode(first, conjugated, max_delay_samples=0)
    assert conjugate_evidence.conjugate_score == pytest.approx(1.0, abs=1e-15)
    assert conjugate_evidence.score < 0.3


def test_suite_publishes_three_aligned_pair_scores_and_separate_firings() -> None:
    first = periodic_tone(256)
    second = [complex(-value.imag, value.real) for value in first]
    fixture = SegmentFixture(paired_bytes(first, second), 64_000)
    view, recording_ref = make_view(fixture)
    config = DetectorSuiteConfig(
        window_samples=64,
        stride_samples=64,
        periodic_lag_samples=8,
        max_pair_delay_samples=0,
    )
    bundle = IndependentDetectorSuite(config, execution_context()).analyze(
        as_view(view), request(recording_ref, config)
    )

    assert len(bundle.observations) == 4 * 5
    assert len(bundle.method_scores) == 4 * 3
    windows = {
        (
            score.segment_id,
            score.receiver_key,
            score.window_start_sample,
            score.window_stop_sample,
        )
        for score in bundle.method_scores
    }
    assert len(windows) == 4
    assert all(
        sum(
            (score.window_start_sample, score.window_stop_sample) == (start, stop)
            for score in bundle.method_scores
        )
        == 3
        for _, _, start, stop in windows
    )

    rule = ThresholdRule(
        "rule_fixture",
        "dataset_fixture",
        (
            ("coarse-energy@0.1.0", 10.0),
            ("paired-common-mode@0.1.0", 0.9),
            ("periodic-coherence@0.1.0", 0.9),
        ),
    )
    firings = apply_threshold_rule(bundle.method_scores, rule)
    assert all(item.fired for item in firings)
    assert all(item.rule_digest == rule.digest for item in firings)
    report = method_firing_association(bundle.method_scores, dict(rule.thresholds))
    assert report.method_ids == (
        "coarse-energy@0.1.0",
        "paired-common-mode@0.1.0",
        "periodic-coherence@0.1.0",
    )
    assert report.shared_window_count == ((4, 4, 4), (4, 4, 4), (4, 4, 4))


def test_deterministic_integer_noise_null_and_channel_swap() -> None:
    first = integer_noise(128, 11)
    second = integer_noise(128, 19)
    fixture = SegmentFixture(paired_bytes(first, second), 128_000)
    config = DetectorSuiteConfig(
        window_samples=128,
        stride_samples=128,
        periodic_lag_samples=16,
        max_pair_delay_samples=2,
    )
    first_view, first_ref = make_view(fixture)
    second_view, _ = make_view(fixture)
    analyzer = IndependentDetectorSuite(config, execution_context())
    expected = analyzer.analyze(as_view(first_view), request(first_ref, config))
    assert (
        analyzer.analyze(as_view(second_view), request(first_ref, config)) == expected
    )
    by_method = {score.method_id: score.score for score in expected.method_scores}
    assert by_method["periodic-coherence"] < 0.2
    assert by_method["paired-common-mode"] < 0.3

    swapped = paired_common_mode(second, first, max_delay_samples=2)
    original = paired_common_mode(first, second, max_delay_samples=2)
    assert swapped.score == pytest.approx(original.score, abs=1e-15)
    assert swapped.delay_samples == -original.delay_samples


def test_drift_moves_coarse_candidate_between_independent_windows() -> None:
    first = periodic_tone(64, period=8) + periodic_tone(64, period=4)
    second = [2 * value for value in first]
    fixture = SegmentFixture(paired_bytes(first, second), 64_000)
    view, recording_ref = make_view(fixture)
    config = DetectorSuiteConfig(
        window_samples=64,
        stride_samples=64,
        periodic_lag_samples=8,
        max_pair_delay_samples=0,
    )
    bundle = IndependentDetectorSuite(config, execution_context()).analyze(
        as_view(view), request(recording_ref, config)
    )
    energy = [
        item
        for item in bundle.observations
        if item.method_id == "coarse-energy" and str(item.receiver_chain_id) == "rx_0"
    ]
    assert [item.frequency_offset_hz for item in energy] == [8_000, 16_000]


@pytest.mark.parametrize(
    ("samples", "reason"),
    [
        ([0j] * 64, "detector-refused-zero-energy"),
        ([complex(32767, 0)] + periodic_tone(63), "detector-refused-clipping"),
    ],
)
def test_scientifically_invalid_windows_are_refused(
    samples: list[complex], reason: str
) -> None:
    view, recording_ref = make_view(
        SegmentFixture(paired_bytes(samples, samples), 64_000)
    )
    config = DetectorSuiteConfig(
        window_samples=64, stride_samples=64, periodic_lag_samples=8
    )
    bundle = IndependentDetectorSuite(config, execution_context()).analyze(
        as_view(view), request(recording_ref, config)
    )
    assert not bundle.observations
    assert not bundle.method_scores
    assert reason in bundle.reason_codes


def test_short_data_and_bad_request_refuse_without_ambiguous_rows() -> None:
    samples = periodic_tone(32)
    view, recording_ref = make_view(
        SegmentFixture(paired_bytes(samples, samples), 64_000)
    )
    config = DetectorSuiteConfig(
        window_samples=64, stride_samples=64, periodic_lag_samples=8
    )
    analyzer = IndependentDetectorSuite(config, execution_context())
    good_request = request(recording_ref, config)
    bundle = analyzer.analyze(as_view(view), good_request)
    assert bundle.reason_codes == ("detectors-skipped-short-segment",)
    assert not bundle.method_scores

    bad = replace(
        good_request,
        config_ref=replace(good_request.config_ref, digest=Digest.sha256(b"wrong")),
    )
    with pytest.raises(ValueError, match="config_ref"):
        analyzer.analyze(as_view(view), bad)


def test_truncated_reader_is_a_hard_input_error() -> None:
    samples = periodic_tone(64)
    view, recording_ref = make_view(
        SegmentFixture(paired_bytes(samples, samples), 64_000)
    )
    view.truncate_reads = True
    config = DetectorSuiteConfig(
        window_samples=64, stride_samples=64, periodic_lag_samples=8
    )
    with pytest.raises(ValueError, match="returned .* expected"):
        IndependentDetectorSuite(config, execution_context()).analyze(
            as_view(view), request(recording_ref, config)
        )


def test_thresholds_do_not_change_extraction_identity() -> None:
    config = DetectorSuiteConfig(
        window_samples=64, stride_samples=64, periodic_lag_samples=8
    )
    assert detector_suite_config_ref(config) == detector_suite_config_ref(config)
    low = ThresholdRule("rule_low", "dataset_a", (("coarse-energy@0.1.0", 1.0),))
    high = ThresholdRule("rule_high", "dataset_a", (("coarse-energy@0.1.0", 100.0),))
    assert low.digest != high.digest
    with pytest.raises(ValueError, match="no entry"):
        apply_threshold_rule(
            (
                MethodScore(
                    "periodic-coherence",
                    "0.1.0",
                    SegmentId("seg_0"),
                    "rxpair_rx_0_rx_1",
                    0,
                    64,
                    0.5,
                    "fixture",
                ),
            ),
            low,
        )
