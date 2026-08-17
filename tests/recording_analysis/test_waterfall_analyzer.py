from __future__ import annotations

import json
import math
import struct
from dataclasses import replace

import pytest

from leo_flow.analysis.recording import (
    BoundedWaterfallAnalyzerV0_1,
    MalformedWaterfallError,
    WaterfallConfigV0_1,
    decode_waterfall_bundle,
    encode_waterfall_bundle,
    waterfall_algorithm_ref_v0_1,
    waterfall_config_ref_v0_1,
)
from leo_flow.analysis.recording.api import (
    AnalysisConfigurationError,
    AnalysisInputError,
)
from leo_flow.analysis.recording.waterfall_codec import (
    MAX_WATERFALL_BUNDLE_BYTES,
)
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
)

from .fakes import SegmentFixture, execution_context, make_view


def _tone_bytes(sample_count: int, sample_rate_hz: int) -> bytes:
    values: list[int] = []
    for index in range(sample_count):
        angle = 2.0 * math.pi * 2_000 * index / sample_rate_hz
        rx0_i = round(1000 * math.cos(angle))
        rx0_q = round(1000 * math.sin(angle))
        rx1_i = round(700 * math.cos(-angle))
        rx1_q = round(700 * math.sin(-angle))
        values.extend((rx0_i, rx0_q, rx1_i, rx1_q))
    return struct.pack(f"<{len(values)}h", *values)


def _request(recording_ref, config, *, dependencies=()):
    return WaterfallAnalysisRequestV0_1(
        SchemaRef(WaterfallAnalysisRequestV0_1.SCHEMA_ID),
        recording_ref.recording_id,
        recording_ref,
        waterfall_algorithm_ref_v0_1(),
        waterfall_config_ref_v0_1(config),
        dependencies,
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
    )


def test_waterfall_has_exact_axes_identities_and_deterministic_power() -> None:
    config = WaterfallConfigV0_1(
        fft_window_samples=16,
        frequency_bins=8,
        maximum_time_bins_per_tile=4,
    )
    view, recording_ref = make_view(
        SegmentFixture(_tone_bytes(160, 16_000), 16_000, 1_500_000_000)
    )
    request = _request(recording_ref, config)
    analyzer = BoundedWaterfallAnalyzerV0_1(config, execution_context())

    first = analyzer.analyze_waterfall(view, request)
    second_view, _ = make_view(
        SegmentFixture(_tone_bytes(160, 16_000), 16_000, 1_500_000_000)
    )
    second = analyzer.analyze_waterfall(second_view, request)

    assert first == second
    assert first.recording_id == recording_ref.recording_id
    assert first.input_recording_identity_digest == recording_ref.identity_digest()
    assert len(first.tiles) == 2
    assert first.tiles[0].power_reference == "counts-squared-per-bin"
    assert [row.start_sample for row in first.tiles[0].time_bins] == [0, 48, 96, 144]
    assert first.tiles[0].frequency_bin_offsets_hz == (
        -7500.0,
        -5500.0,
        -3500.0,
        -1500.0,
        500.0,
        2500.0,
        4500.0,
        6500.0,
    )
    assert max(range(8), key=first.tiles[0].time_bins[0].power_db.__getitem__) == 5
    assert max(range(8), key=first.tiles[1].time_bins[0].power_db.__getitem__) == 3
    # Both receiver tiles are computed from each IQ read, not by rereading CAS.
    assert len(view.calls) == 4
    assert all(
        stop - start == analyzer.maximum_read_samples for _, start, stop in view.calls
    )


def test_total_cell_bound_reduces_time_rows_before_reading() -> None:
    config = WaterfallConfigV0_1(
        fft_window_samples=16,
        frequency_bins=8,
        maximum_time_bins_per_tile=8,
        maximum_total_cells=32,
    )
    view, recording_ref = make_view(SegmentFixture(_tone_bytes(160, 16_000), 16_000))
    bundle = BoundedWaterfallAnalyzerV0_1(
        config, execution_context()
    ).analyze_waterfall(view, _request(recording_ref, config))
    assert (
        sum(
            len(tile.time_bins) * len(tile.frequency_bin_offsets_hz)
            for tile in bundle.tiles
        )
        == 32
    )
    assert all(len(tile.time_bins) == 2 for tile in bundle.tiles)
    assert len(view.calls) == 2


def test_identity_and_configuration_fail_before_iq_read() -> None:
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    view, recording_ref = make_view(SegmentFixture(_tone_bytes(32, 16_000), 16_000))
    request = _request(recording_ref, config)
    bad = replace(
        request,
        algorithm_ref=ArtifactRef(
            waterfall_algorithm_ref_v0_1().artifact_id,
            Digest.sha256(b"wrong"),
            waterfall_algorithm_ref_v0_1().schema,
        ),
    )
    with pytest.raises(AnalysisConfigurationError, match="algorithm_ref"):
        BoundedWaterfallAnalyzerV0_1(config, execution_context()).analyze_waterfall(
            view, bad
        )
    assert not view.calls


@pytest.mark.parametrize("mutable", [False, True])
def test_malformed_reader_result_fails_explicitly(mutable: bool) -> None:
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    view, recording_ref = make_view(SegmentFixture(_tone_bytes(32, 16_000), 16_000))
    view.truncate_reads = not mutable
    view.mutable_result = mutable
    with pytest.raises(AnalysisInputError, match="expected|immutable"):
        BoundedWaterfallAnalyzerV0_1(config, execution_context()).analyze_waterfall(
            view, _request(recording_ref, config)
        )


def test_codec_round_trip_is_canonical_and_bounded() -> None:
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    view, recording_ref = make_view(SegmentFixture(_tone_bytes(32, 16_000), 16_000))
    bundle = BoundedWaterfallAnalyzerV0_1(
        config, execution_context()
    ).analyze_waterfall(view, _request(recording_ref, config))
    payload = encode_waterfall_bundle(bundle)
    assert decode_waterfall_bundle(payload) == bundle
    assert encode_waterfall_bundle(decode_waterfall_bundle(payload)) == payload
    assert len(payload) < MAX_WATERFALL_BUNDLE_BYTES


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"x":1,"x":2}', "duplicate"),
        (b"[]", "root"),
        (b"0" * (MAX_WATERFALL_BUNDLE_BYTES + 1), "size"),
    ],
)
def test_codec_rejects_ambiguous_or_oversized_bytes(
    payload: bytes, message: str
) -> None:
    with pytest.raises(MalformedWaterfallError, match=message):
        decode_waterfall_bundle(payload)


def test_codec_rejects_unknown_fields() -> None:
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    view, recording_ref = make_view(SegmentFixture(_tone_bytes(32, 16_000), 16_000))
    bundle = BoundedWaterfallAnalyzerV0_1(
        config, execution_context()
    ).analyze_waterfall(view, _request(recording_ref, config))
    document = json.loads(encode_waterfall_bundle(bundle))
    document["invented"] = True
    from leo_flow.contracts.core import canonical_json_bytes

    with pytest.raises(MalformedWaterfallError, match="fields"):
        decode_waterfall_bundle(canonical_json_bytes(document))


@pytest.mark.parametrize(
    "values, message",
    [
        ({"fft_window_samples": 12}, "fft_window"),
        ({"frequency_bins": 256}, "frequency_bins"),
        ({"maximum_time_bins_per_tile": 129}, "public contract"),
        ({"maximum_tiles": 65}, "public contract"),
        ({"maximum_total_cells": 262_145}, "public contract"),
        ({"power_floor_counts_squared": float("nan")}, "power_floor"),
    ],
)
def test_config_rejects_values_outside_public_bounds(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        WaterfallConfigV0_1(**values)
