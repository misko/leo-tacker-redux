from __future__ import annotations

from dataclasses import replace
from typing import cast

import numpy as np

from leo_flow.analysis.recording.starlink_pilot_prescreen import (
    CompleteIqPilotPrescreenAnalyzerV0_1,
    ofdm_periodicity_v0_1,
)
from leo_flow.contracts.core import V0_1, RadioId, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenPlanV0_1,
    StarlinkPilotPrescreenRequestV0_1,
)
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view


def _ci16(left: np.ndarray, right: np.ndarray) -> bytes:
    interleaved = np.empty((len(left), 2, 2), dtype="<i2")
    for receiver, values in enumerate((left, right)):
        interleaved[:, receiver, 0] = np.clip(np.rint(values.real), -32000, 32000)
        interleaved[:, receiver, 1] = np.clip(np.rint(values.imag), -32000, 32000)
    return interleaved.tobytes()


def _cp_signal(sample_count: int, *, amplitude: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    useful = 10
    symbols = []
    while sum(len(item) for item in symbols) < sample_count:
        body = rng.normal(size=useful) + 1j * rng.normal(size=useful)
        symbols.append(np.concatenate((body[-1:], body)))
    return amplitude * np.concatenate(symbols)[:sample_count]


def test_ofdm_periodicity_separates_cyclic_prefix_from_seeded_noise() -> None:
    rng = np.random.default_rng(41)
    signal = _cp_signal(20_000, amplitude=200.0, seed=42)
    noise = 200.0 * (rng.normal(size=20_000) + 1j * rng.normal(size=20_000))

    signal_score, _phase, useful, total = ofdm_periodicity_v0_1(
        np.asarray(signal, dtype=np.complex128), 2_500_000.0
    )
    noise_score, *_ = ofdm_periodicity_v0_1(
        np.asarray(noise, dtype=np.complex128), 2_500_000.0
    )

    assert (useful, total) == (10, 11)
    assert signal_score > 0.95
    assert noise_score < 0.10


def test_complete_prescreen_tiles_tail_and_selects_low_power_periodic_window() -> None:
    rng = np.random.default_rng(43)
    tile = 20_000
    noise_high = 600.0 * (rng.normal(size=tile) + 1j * rng.normal(size=tile))
    periodic_low = _cp_signal(tile, amplitude=120.0, seed=44)
    tail = 300.0 * (rng.normal(size=123) + 1j * rng.normal(size=123))
    rx0 = np.concatenate((noise_high, periodic_low, tail))
    rx1 = np.concatenate((noise_high, noise_high, tail))
    data = _ci16(rx0, rx1)
    original, recording_ref = make_view(SegmentFixture(data, 2_500_000))
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested, tags=(("channel", "4"), ("edge", "lower"))
        ),
    )
    view = FakeRecordingView(
        replace(
            original.manifest, radio_id=RadioId("radio_current"), segments=(tagged,)
        ),
        {segment.segment_id: data},
    )
    selections = tuple(
        FullDwellTimelineStreamSelectionV0_1(
            RadioId("radio_current"),
            f"lnb-current-{index}",
            segment.segment_id,
            receiver,
            4,
            StarlinkEdge.LOWER,
            2_500_000.0,
            segment.sample_count,
        )
        for index, receiver in enumerate(segment.requested.receiver_chain_ids)
    )
    request = StarlinkPilotPrescreenRequestV0_1(
        SchemaRef(StarlinkPilotPrescreenRequestV0_1.SCHEMA_ID, V0_1),
        recording_ref.recording_id,
        recording_ref,
        StarlinkPilotPrescreenPlanV0_1(tile, 10, 1, 1),
        selections,
    )

    result = CompleteIqPilotPrescreenAnalyzerV0_1(execution_context()).analyze(
        cast(RecordingView, view), request
    )

    first = result.streams[0]
    assert tuple((item.start_sample, item.stop_sample) for item in first.windows) == (
        (0, tile),
        (tile, 2 * tile),
        (2 * tile, 2 * tile + 123),
    )
    assert first.windows[0].power_rank == 0
    assert first.windows[1].periodicity_rank == 0
    assert first.windows[1].power_rank is None
    assert first.analyzed_sample_count == len(rx0)
    assert first.coverage_fraction == 1.0
    assert result.candidate_only
    assert result.calibrated_detection_count is None
    assert len(view.calls) == 3
