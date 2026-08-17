from __future__ import annotations

from dataclasses import replace

from leo_flow.adapters.dashboard_doppler_aggregate import (
    MAXIMUM_POINTS_PER_SERIES,
    _decimate_points,
    _summaries,
    _tile_evidence,
)
from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    AdvancedBlindDopplerAnalyzerV0_1,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_doppler_aggregate import (
    DopplerAggregateQueryV0_1,
    DopplerAggregateTrackPointV0_1,
)
from tests.recording_analysis.test_waterfall_doppler_pipeline import _basic, _bundle


def _row(radio: str, receiver: str) -> dict[str, object]:
    return {
        "recording_id": f"rec_{radio}_{receiver}",
        "radio_id": radio,
        "receiver_chain_id": receiver,
        "segment_id": "seg_focus_ch4_lower",
        "doppler_id": "doppler_" + ("1" if radio == "radio_a" else "2") * 32,
        "waterfall_product_id": "waterfall_" + "3" * 32,
        "basic_bundle_digest_algorithm": "sha256",
        "basic_bundle_digest_value": "4" * 64,
        "advanced_bundle_digest_algorithm": "sha256",
        "advanced_bundle_digest_value": "5" * 64,
        "started_utc_ns": 1_000,
    }


def _query(**changes: object) -> DopplerAggregateQueryV0_1:
    base: dict[str, object] = {
        "start_utc_ns": UtcNs(1),
        "stop_utc_ns": UtcNs(30_000_000_000),
    }
    base.update(changes)
    return DopplerAggregateQueryV0_1(**base)  # type: ignore[arg-type]


def test_four_radio_receiver_sources_are_never_pooled() -> None:
    spectrogram, basic = _basic(_bundle())
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    all_series = []
    for radio in ("radio_a", "radio_b"):
        for receiver in ("rx_lnb_a", "rx_lnb_b"):
            series, _ = _tile_evidence(_row(radio, receiver), basic, advanced, _query())
            all_series.extend(series)

    basic_series = [item for item in all_series if item.method == "basic"]
    assert {(item.radio_id, item.receiver_chain_id) for item in basic_series} == {
        ("radio_a", "rx_lnb_a"),
        ("radio_a", "rx_lnb_b"),
        ("radio_b", "rx_lnb_a"),
        ("radio_b", "rx_lnb_b"),
    }
    summaries = _summaries(all_series)
    assert len({(item.radio_id, item.receiver_chain_id) for item in summaries}) == 4


def test_radio_receiver_method_model_and_association_filters_are_conjunctive() -> None:
    spectrogram, basic = _basic(_bundle())
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    series, controls = _tile_evidence(
        _row("radio_a", "rx_lnb_b"),
        basic,
        advanced,
        _query(
            methods=("advanced",),
            models=("slope-bank",),
            radio_ids=("radio_a",),
            receiver_chain_ids=("rx_lnb_b",),
            channels=("CH4",),
            edges=("lower",),
            association_states=("matched-basic-candidate",),
        ),
    )

    assert len(series) == 1
    assert series[0].method == "advanced"
    assert series[0].points == ()
    assert {item.control_class for item in controls} == {
        "heldout-path",
        "stationary",
        "opposite-slope",
        "time-shuffle",
    }
    excluded, excluded_controls = _tile_evidence(
        _row("radio_b", "rx_lnb_b"),
        basic,
        advanced,
        _query(radio_ids=("radio_a",)),
    )
    assert excluded == excluded_controls == []


def test_advanced_path_only_keeps_identity_without_inventing_frequency() -> None:
    spectrogram, populated = _basic(_bundle())
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, populated)
    assert advanced.association is not None and advanced.slope_bank is not None
    basic = replace(populated, candidates=())
    advanced = replace(
        advanced,
        slope_bank=replace(advanced.slope_bank, basic_candidate_rank=None),
        association=replace(
            advanced.association,
            state="advanced-path-only",
            basic_candidate_rank=None,
            overlap_point_count=0,
            overlap_fraction=0.0,
            mean_frequency_distance_hz=None,
            maximum_frequency_distance_hz=None,
        ),
    )

    series, _ = _tile_evidence(_row("radio_a", "rx_lnb_a"), basic, advanced, _query())

    assert len(series) == 1
    assert series[0].association_state == "advanced-path-only"
    assert series[0].reference_frequency_hz is None
    assert series[0].points == ()


def test_track_decimation_preserves_endpoints_and_frequency_extrema() -> None:
    points = tuple(
        DopplerAggregateTrackPointV0_1(
            UtcNs(index + 1),
            float(index),
            -500.0 if index == 129 else 900.0 if index == 131 else float(index),
        )
        for index in range(300)
    )

    result = _decimate_points(points)

    assert len(result) == MAXIMUM_POINTS_PER_SERIES
    assert result[0] == points[0] and result[-1] == points[-1]
    assert points[129] in result and points[131] in result
