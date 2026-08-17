from __future__ import annotations

import math
import random
from collections.abc import Callable

import pytest

from leo_flow.analysis.tracking.blind_doppler import (
    BasicBlindDopplerAnalyzer,
    BlindDopplerConfig,
    blind_doppler_config_digest,
)
from leo_flow.contracts.blind_doppler import (
    BlindDopplerAnalysisRequestV0_1,
    BlindDopplerBundleV0_1,
    DopplerPolynomialOrder,
    SpectrogramRowV0_1,
    SpectrogramSliceV0_1,
)
from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    UtcNs,
)

START_NS = 1_800_000_000_000_000_000
CENTER_HZ = 11_325_000_000.0
AXIS = tuple(float(value) for value in range(-50_000, 50_001, 1_000))
IDENTITY = Digest.sha256(b"synthetic-spectrogram")


def _slice(
    tracks: tuple[Callable[[float], float | None], ...] = (),
    *,
    row_count: int = 24,
    agc: Callable[[int], float] = lambda _: 0.0,
    broadband_rows: frozenset[int] = frozenset(),
    noise_amplitude: float = 0.2,
) -> SpectrogramSliceV0_1:
    randomizer = random.Random(8317)
    rows = []
    for row_index in range(row_count):
        time_s = row_index * 0.01
        floor = agc(row_index)
        power = [
            floor + randomizer.uniform(-noise_amplitude, noise_amplitude) for _ in AXIS
        ]
        if row_index in broadband_rows:
            for index in range(31):
                power[index] += 12.0
        for track in tracks:
            frequency = track(time_s)
            if frequency is None:
                continue
            for index, offset in enumerate(AXIS):
                distance_bins = (offset - frequency) / 1_000
                power[index] += 18.0 * math.exp(-0.5 * (distance_bins / 0.65) ** 2)
        rows.append(
            SpectrogramRowV0_1(
                midpoint_utc_ns=UtcNs(START_NS + row_index * 10_000_000),
                power_db=tuple(power),
            )
        )
    return SpectrogramSliceV0_1(
        schema=SchemaRef(SpectrogramSliceV0_1.SCHEMA_ID),
        input_identity_digest=IDENTITY,
        segment_id=SegmentId("seg_synthetic"),
        receiver_chain_id=ReceiverChainId("rx_test"),
        center_frequency_hz=CENTER_HZ,
        frequency_bin_offsets_hz=AXIS,
        power_reference="synthetic-db",
        rows=tuple(rows),
    )


def _analyze(
    spectrogram: SpectrogramSliceV0_1, *, top_k: int = 8
) -> BlindDopplerBundleV0_1:
    config = BlindDopplerConfig(
        minimum_spectral_peak_excess_db=5.0,
        maximum_frequency_step_hz=1_250,
        maximum_abs_drift_rate_hz_s=120_000,
    )
    request = BlindDopplerAnalysisRequestV0_1(
        schema=SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
        input_identity_digest=IDENTITY,
        config_digest=blind_doppler_config_digest(config),
        max_candidates=top_k,
    )
    return BasicBlindDopplerAnalyzer(config).analyze_blind_doppler(spectrogram, request)


def _selected(candidate):
    return next(fit for fit in candidate.fits if fit.order is candidate.selected_order)


def _constant_track(offset: float) -> Callable[[float], float]:
    return lambda _: offset


def test_stationary_line_prefers_stationary_control() -> None:
    result = _analyze(_slice((lambda _: 7_350.0,)))
    candidate = result.candidates[0]

    assert candidate.selected_order is DopplerPolynomialOrder.CONSTANT
    assert not candidate.stationary_control.moving_model_preferred
    assert len(candidate.points) == 24
    assert candidate.mean_spectral_peak_excess_db > 15


@pytest.mark.parametrize("rate", [45_000.0, -45_000.0])
def test_linear_chirps_recover_direction_and_sub_bin_rate(rate: float) -> None:
    result = _analyze(_slice((lambda time: -4_200.0 + rate * time,)))
    candidate = result.candidates[0]
    fit = _selected(candidate)

    assert candidate.selected_order in (
        DopplerPolynomialOrder.LINEAR,
        DopplerPolynomialOrder.QUADRATIC,
    )
    assert fit.drift_rate_hz_s == pytest.approx(rate, rel=0.08)
    assert any(point.interpolated_bin % 1 for point in candidate.points)
    assert candidate.stationary_control.moving_model_preferred


def test_quadratic_chirp_exposes_acceleration() -> None:
    result = _analyze(
        _slice((lambda time: -8_000 + 5_000 * time + 0.5 * 180_000 * time**2,))
    )
    candidate = result.candidates[0]
    fit = _selected(candidate)

    assert candidate.selected_order is DopplerPolynomialOrder.QUADRATIC
    assert fit.drift_acceleration_hz_s2 == pytest.approx(180_000, rel=0.20)


def test_intermittent_track_links_across_missing_rows() -> None:
    missing = {5, 6, 13}
    result = _analyze(
        _slice(
            (
                lambda time: (
                    None if round(time / 0.01) in missing else -2_000 + 20_000 * time
                ),
            )
        )
    )
    candidate = result.candidates[0]

    assert candidate.missing_row_count == len(missing)
    assert len(candidate.points) == 24 - len(missing)


def test_crossing_tracks_remain_two_motion_candidates() -> None:
    result = _analyze(
        _slice(
            (
                lambda time: -8_000 + 70_000 * time,
                lambda time: 8_000 - 70_000 * time,
            )
        )
    )
    rates = sorted(
        _selected(candidate).drift_rate_hz_s for candidate in result.candidates[:4]
    )

    assert any(rate < -50_000 for rate in rates)
    assert any(rate > 50_000 for rate in rates)


def test_agc_step_is_removed_by_per_row_noise_control() -> None:
    result = _analyze(
        _slice(
            (lambda time: 3_000 + 30_000 * time,),
            agc=lambda row: 18.0 if row >= 12 else -7.0,
        )
    )
    candidate = result.candidates[0]

    assert len(candidate.points) == 24
    assert _selected(candidate).drift_rate_hz_s == pytest.approx(30_000, rel=0.10)


def test_broadband_rows_are_suppressed_without_breaking_track() -> None:
    result = _analyze(
        _slice((lambda time: -4_000 + 10_000 * time,), broadband_rows=frozenset({8, 9}))
    )

    assert result.warnings == ("broadband_rows_suppressed:2",)
    assert result.candidates[0].missing_row_count == 2


def test_edge_truncation_is_explicit_evidence() -> None:
    result = _analyze(_slice((lambda _: AXIS[0],)))

    assert result.candidates[0].edge_truncated_point_count == 24
    assert all(point.edge_truncated for point in result.candidates[0].points)


def test_noise_only_produces_no_candidates() -> None:
    result = _analyze(_slice(noise_amplitude=1.0))

    assert result.candidates == ()
    assert result.reason_codes == ("no_candidate_met_track_bounds",)


def test_output_is_bounded_by_request_top_k() -> None:
    tracks = tuple(
        _constant_track(float(value)) for value in range(-32_000, 32_001, 16_000)
    )
    result = _analyze(_slice(tracks), top_k=3)

    assert len(result.candidates) == 3
    assert tuple(candidate.rank for candidate in result.candidates) == (1, 2, 3)


def test_input_contract_rejects_non_monotonic_rows() -> None:
    valid = _slice()
    with pytest.raises(ValueError, match="increasing time"):
        SpectrogramSliceV0_1(
            schema=valid.schema,
            input_identity_digest=valid.input_identity_digest,
            segment_id=valid.segment_id,
            receiver_chain_id=valid.receiver_chain_id,
            center_frequency_hz=valid.center_frequency_hz,
            frequency_bin_offsets_hz=valid.frequency_bin_offsets_hz,
            power_reference=valid.power_reference,
            rows=(valid.rows[1], valid.rows[0]),
        )


def test_request_must_close_over_exact_analyzer_config() -> None:
    spectrogram = _slice((lambda _: 0.0,))
    request = BlindDopplerAnalysisRequestV0_1(
        schema=SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
        input_identity_digest=IDENTITY,
        config_digest=Digest.sha256(b"different-config"),
        max_candidates=1,
    )

    with pytest.raises(ValueError, match="config digest"):
        BasicBlindDopplerAnalyzer().analyze_blind_doppler(spectrogram, request)
