from __future__ import annotations

import json
import math
import struct
from dataclasses import replace

import pytest

from leo_flow.analysis.recording import (
    FullCoverageWaterfallAnalyzerV0_2,
    MalformedWaterfallError,
    NumpySpectrumPowerBackendV0_2,
    Radix2SpectrumPowerBackendV0_2,
    WaterfallConfigV0_1,
    WaterfallConfigV0_2,
    decode_waterfall_bundle_v0_2,
    encode_waterfall_bundle_v0_2,
    waterfall_algorithm_ref_v0_2,
    waterfall_config_ref_v0_2,
)
from leo_flow.analysis.recording.api import AnalysisInputError
from leo_flow.contracts.continuity import ContiguousRfSpan
from leo_flow.contracts.core import SchemaRef, canonical_json_bytes
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
)

from .fakes import SegmentFixture, execution_context, make_view


class MarkerPowerBackend:
    """Treat the first CI16 component as already-linear synthetic power."""

    backend_id = "test-marker-linear-power"

    def frame_power(
        self,
        raw_ci16: bytes,
        *,
        receiver_count: int,
        fft_window_samples: int,
        display_frequency_bins: int,
    ) -> tuple[tuple[float, ...], ...]:
        marker = float(struct.unpack_from("<h", raw_ci16)[0])
        return tuple(
            tuple(
                marker * (frequency + 1) for frequency in range(display_frequency_bins)
            )
            for _ in range(receiver_count)
        )


def _marker_bytes(markers: tuple[int, ...], frame_samples: int) -> bytes:
    values: list[int] = []
    for marker in markers:
        for _ in range(frame_samples):
            values.extend((marker, 0, marker, 0))
    return struct.pack(f"<{len(values)}h", *values)


def _tone_frames(
    frequencies_hz: tuple[float, ...], frame_samples: int, sample_rate_hz: int
) -> bytes:
    values: list[int] = []
    for frequency in frequencies_hz:
        for index in range(frame_samples):
            angle = 2.0 * math.pi * frequency * index / sample_rate_hz
            i_value = round(1_000 * math.cos(angle))
            q_value = round(1_000 * math.sin(angle))
            values.extend((i_value, q_value, i_value, q_value))
    return struct.pack(f"<{len(values)}h", *values)


def _request(recording_ref, config: WaterfallConfigV0_2):
    return WaterfallAnalysisRequestV0_2(
        SchemaRef(WaterfallAnalysisRequestV0_2.SCHEMA_ID, V0_2),
        recording_ref.recording_id,
        recording_ref,
        waterfall_algorithm_ref_v0_2(),
        waterfall_config_ref_v0_2(config),
        (),
        SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
    )


def test_v0_2_aggregates_all_frames_in_linear_power_and_reports_coverage() -> None:
    config = WaterfallConfigV0_2(
        fft_window_samples=8,
        display_frequency_bins=4,
        target_time_bins_per_tile=4,
        high_percentile=100.0,
        numerical_backend_id="test-marker-linear-power",
    )
    # Eight complete frames plus two tail samples: no sparse time sampling.
    data = _marker_bytes((1, 9, 25, 49, 81, 121, 169, 225), 8)
    data += _marker_bytes((7,), 2)
    view, recording_ref = make_view(SegmentFixture(data, 800))
    analyzer = FullCoverageWaterfallAnalyzerV0_2(
        config, execution_context(), MarkerPowerBackend()
    )

    bundle = analyzer.analyze_waterfall(view, _request(recording_ref, config))

    assert bundle.schema == SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2)
    assert len(bundle.tiles) == 2
    tile = bundle.tiles[0]
    assert tile.coverage.contiguous_rf_sample_count == 66
    assert tile.coverage.analyzed_sample_count == 64
    assert tile.coverage.discarded_tail_sample_count == 2
    assert tile.coverage.fft_frame_count == 8
    assert tile.coverage.coverage_fraction == pytest.approx(64 / 66)
    assert [row.fft_frame_count for row in tile.time_bins] == [2, 2, 2, 2]
    assert tile.time_bins[0].average_power_db[0] == pytest.approx(
        10 * math.log10((1 + 9) / 2), abs=1e-6
    )
    assert tile.time_bins[0].high_percentile_power_db[0] == pytest.approx(
        10 * math.log10(9), abs=1e-6
    )
    for frequency_index in range(4):
        assert statistics_median(
            row.temporal_median_residual_db[frequency_index] for row in tile.time_bins
        ) == pytest.approx(0.0, abs=1e-6)
    assert len(view.calls) == 8
    assert all(
        stop - start == analyzer.maximum_read_samples for _, start, stop in view.calls
    )


def statistics_median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (ordered[middle - 1] + ordered[middle]) / 2


def test_v0_2_preserves_discontinuities_in_rows_and_exact_sample_accounting() -> None:
    config = WaterfallConfigV0_2(
        fft_window_samples=8,
        display_frequency_bins=4,
        target_time_bins_per_tile=2,
        numerical_backend_id="test-marker-linear-power",
    )
    view, recording_ref = make_view(
        SegmentFixture(_marker_bytes(tuple(range(1, 8)), 8), 800)
    )
    view.contiguous_rf_spans = lambda segment_id: (  # type: ignore[method-assign]
        ContiguousRfSpan(0, 18, 0, 18),
        ContiguousRfSpan(30, 50, 30, 50),
    )
    bundle = FullCoverageWaterfallAnalyzerV0_2(
        config, execution_context(), MarkerPowerBackend()
    ).analyze_waterfall(view, _request(recording_ref, config))

    tile = bundle.tiles[0]
    assert tile.coverage.contiguous_rf_span_count == 2
    assert tile.coverage.contiguous_rf_sample_count == 38
    assert tile.coverage.analyzed_sample_count == 32
    assert tile.coverage.discarded_tail_sample_count == 6
    assert [(row.start_sample, row.stop_sample) for row in tile.time_bins] == [
        (0, 16),
        (30, 46),
    ]
    assert [row.fft_frame_start_samples for row in tile.time_bins] == [
        (0, 8),
        (30, 38),
    ]
    assert [start for _, start, _ in view.calls] == [0, 8, 30, 38]


def test_dependency_free_fft_backend_exposes_a_moving_tone_ridge() -> None:
    frame_samples = 16
    sample_rate_hz = 16_000
    frequencies = (-6_000.0, -2_000.0, 2_000.0, 6_000.0)
    config = WaterfallConfigV0_2(
        fft_window_samples=frame_samples,
        display_frequency_bins=8,
        target_time_bins_per_tile=4,
        high_percentile=100.0,
        numerical_backend_id="stdlib-radix2-v0.2",
    )
    view, recording_ref = make_view(
        SegmentFixture(
            _tone_frames(frequencies, frame_samples, sample_rate_hz), sample_rate_hz
        )
    )
    bundle = FullCoverageWaterfallAnalyzerV0_2(
        config, execution_context(), Radix2SpectrumPowerBackendV0_2()
    ).analyze_waterfall(view, _request(recording_ref, config))

    peak_bins = [
        max(range(8), key=row.average_power_db.__getitem__)
        for row in bundle.tiles[0].time_bins
    ]
    assert peak_bins == sorted(peak_bins)
    assert len(set(peak_bins)) == 4


def test_numpy_backend_agrees_with_dependency_free_numerical_oracle() -> None:
    pytest.importorskip("numpy")
    raw = _tone_frames((2_000.0,), 32, 32_000)
    kwargs = {
        "receiver_count": 2,
        "fft_window_samples": 32,
        "display_frequency_bins": 8,
    }
    expected = Radix2SpectrumPowerBackendV0_2().frame_power(raw, **kwargs)
    actual = NumpySpectrumPowerBackendV0_2().frame_power(raw, **kwargs)
    for expected_receiver, actual_receiver in zip(expected, actual, strict=True):
        assert actual_receiver == pytest.approx(expected_receiver, rel=1e-12, abs=1e-9)


def test_v0_2_codec_is_canonical_bounded_and_rejects_v0_1_schema() -> None:
    config = WaterfallConfigV0_2(
        fft_window_samples=8,
        display_frequency_bins=4,
        target_time_bins_per_tile=2,
        numerical_backend_id="test-marker-linear-power",
    )
    view, recording_ref = make_view(SegmentFixture(_marker_bytes((1, 3), 8), 800))
    bundle = FullCoverageWaterfallAnalyzerV0_2(
        config, execution_context(), MarkerPowerBackend()
    ).analyze_waterfall(view, _request(recording_ref, config))
    payload = encode_waterfall_bundle_v0_2(bundle)

    assert decode_waterfall_bundle_v0_2(payload) == bundle
    assert (
        encode_waterfall_bundle_v0_2(decode_waterfall_bundle_v0_2(payload)) == payload
    )
    document = json.loads(payload)
    document["schema"]["version"] = {"major": 0, "minor": 1}
    with pytest.raises(MalformedWaterfallError, match="v0.2 schema"):
        decode_waterfall_bundle_v0_2(canonical_json_bytes(document))


def test_v0_2_backend_failure_is_an_explicit_input_error() -> None:
    class BadBackend(MarkerPowerBackend):
        def frame_power(self, *args, **kwargs):
            return ((float("nan"),),)

    config = WaterfallConfigV0_2(
        fft_window_samples=8,
        display_frequency_bins=4,
        target_time_bins_per_tile=1,
        numerical_backend_id="test-marker-linear-power",
    )
    view, recording_ref = make_view(SegmentFixture(_marker_bytes((1,), 8), 800))
    with pytest.raises(AnalysisInputError, match="backend failed"):
        FullCoverageWaterfallAnalyzerV0_2(
            config, execution_context(), BadBackend()
        ).analyze_waterfall(view, _request(recording_ref, config))


def test_v0_2_defaults_match_twenty_second_capture_design_without_changing_v0_1() -> (
    None
):
    config = WaterfallConfigV0_2()
    sample_count = 20 * 2_500_000
    frame_count = sample_count // config.fft_window_samples
    analyzed = frame_count * config.fft_window_samples

    assert config.fft_window_samples == 32_768
    assert config.display_frequency_bins == 512
    assert config.target_time_bins_per_tile == 200
    assert frame_count == 1_525
    assert analyzed / sample_count > 0.999
    assert 2_500_000 / config.fft_window_samples == pytest.approx(76.2939453125)
    assert 2_500_000 / config.display_frequency_bins == pytest.approx(4_882.8125)
    assert WaterfallConfigV0_1() == replace(WaterfallConfigV0_1())
