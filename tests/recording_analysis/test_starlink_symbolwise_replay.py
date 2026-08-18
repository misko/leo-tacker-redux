from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from leo_flow.analysis.recording.starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayAnalyzerV0_1,
    StarlinkSymbolwiseReplayConfigV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_surrogate_null import StarlinkSearchPatternRole
from leo_flow.contracts.starlink_symbolwise_replay import (
    V0_1,
    StarlinkReceiverFrequencyCenterV0_1,
)

from .fakes import execution_context

SAMPLE_RATE_HZ = 2_500_000.0
WINDOW_SAMPLES = 25_000
DEFAULT_RECEIVER_CHAIN_ID = ReceiverChainId("rx_label_c")


class _Reader:
    def __init__(self, values: np.ndarray) -> None:
        self.values = values
        self.calls: list[tuple[int, int]] = []

    def read_window(self, start_sample: int, sample_count: int) -> np.ndarray:
        self.calls.append((start_sample, sample_count))
        return self.values[start_sample : start_sample + sample_count]


def _frequency_center(
    center_hz: float = 0.0, *, path_identity: bytes = b"physical-path-a"
) -> StarlinkReceiverFrequencyCenterV0_1:
    return StarlinkReceiverFrequencyCenterV0_1(
        SchemaRef(StarlinkReceiverFrequencyCenterV0_1.SCHEMA_ID, V0_1),
        "calibration_test_epoch_20260818",
        Digest.sha256(b"immutable-hardware-epoch"),
        Digest.sha256(path_identity),
        ArtifactRef(
            "receiver-center-calibration-source",
            Digest.sha256(b"signed-calibration-source"),
            SchemaRef("org.example.receiver-center-calibration"),
        ),
        center_hz,
        "absolute-cfo-relative-to-recording-if-center",
        True,
    )


def _analyze(
    values: np.ndarray,
    *,
    receiver_chain_id: ReceiverChainId = DEFAULT_RECEIVER_CHAIN_ID,
    center: StarlinkReceiverFrequencyCenterV0_1 | None = None,
):
    reader = _Reader(values)
    result = StarlinkSymbolwiseReplayAnalyzerV0_1(
        StarlinkSymbolwiseReplayConfigV0_1(surrogate_count=1),
        execution_context(),
    ).analyze_receiver(
        reader,
        recording_id=RecordingId("rec_symbolwise_unit"),
        recording_identity_digest=Digest.sha256(b"symbolwise-unit-recording"),
        segment_id=SegmentId("seg_ch4_lower"),
        receiver_chain_id=receiver_chain_id,
        edge=StarlinkEdge.LOWER,
        sample_rate_hz=SAMPLE_RATE_HZ,
        segment_sample_count=len(values),
        frequency_center=center or _frequency_center(),
    )
    return reader, result


def test_full_sixty_second_plan_is_600_exact_windows_and_ten_percent_union() -> None:
    analyzer = StarlinkSymbolwiseReplayAnalyzerV0_1(
        StarlinkSymbolwiseReplayConfigV0_1(), execution_context()
    )

    plan = analyzer.resource_plan(
        sample_rate_hz=SAMPLE_RATE_HZ,
        segment_sample_count=60 * round(SAMPLE_RATE_HZ),
        frequency_center=_frequency_center(602_869.4),
    )

    assert plan.window_sample_count == 25_000
    assert plan.cadence_sample_count == 250_000
    assert len(plan.window_start_samples) == 600
    assert plan.window_start_samples == tuple(range(0, 150_000_000, 250_000))
    assert plan.analyzed_union_sample_count == 15_000_000
    assert plan.coverage_fraction == pytest.approx(0.1)
    assert plan.pattern_count == 5
    assert plan.timing_search_cell_count == 89_991_000
    assert plan.refinement_search_cell_count == 564_000
    assert plan.estimated_maximum_working_bytes == 6_400_000


def test_every_pattern_reads_same_window_and_repeats_the_complete_search() -> None:
    rng = np.random.default_rng(0x51A71E)
    values = (
        rng.normal(size=WINDOW_SAMPLES) + 1j * rng.normal(size=WINDOW_SAMPLES)
    ).astype(np.complex64)

    reader, result = _analyze(values)

    assert reader.calls == [(0, WINDOW_SAMPLES)]
    window = result.windows[0]
    assert tuple(item.pattern.role for item in window.patterns) == (
        StarlinkSearchPatternRole.QIN_EXACT,
        StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE,
    )
    assert window.patterns[1].pattern.codebook_index == 0
    assert len({item.pattern.template_ref for item in window.patterns}) == 2
    assert len({item.selection_control_template_ref for item in window.patterns}) == 2
    assert {
        (item.timing_search_cell_count, item.refinement_search_cell_count)
        for item in window.patterns
    } == {(29_997, 188)}
    assert result.candidates_only is True
    assert "finite-pattern-controls-not-empirical-null" in result.reason_codes


def test_receiver_label_permutation_cannot_change_numerics_when_center_travels() -> (
    None
):
    rng = np.random.default_rng(0xC0FFEE)
    values = (
        rng.normal(size=WINDOW_SAMPLES) + 1j * rng.normal(size=WINDOW_SAMPLES)
    ).astype(np.complex64)
    center = _frequency_center(602_869.4, path_identity=b"immutable-signal-path-c")

    _, labelled_c = _analyze(
        values, receiver_chain_id=ReceiverChainId("rx_lnb_c"), center=center
    )
    _, relabelled_z = _analyze(
        values, receiver_chain_id=ReceiverChainId("rx_permuted_z"), center=center
    )

    assert labelled_c.receiver_chain_id != relabelled_z.receiver_chain_id
    assert labelled_c.frequency_center == relabelled_z.frequency_center
    assert labelled_c.windows == relabelled_z.windows
    assert labelled_c.timing_search_cell_count == relabelled_z.timing_search_cell_count
    assert (
        labelled_c.refinement_search_cell_count
        == relabelled_z.refinement_search_cell_count
    )


def test_frequency_center_must_be_precommitted_and_resource_caps_precede_reads() -> (
    None
):
    with pytest.raises(ValueError, match="fixed before replay"):
        replace(_frequency_center(), data_independent=False)

    analyzer = StarlinkSymbolwiseReplayAnalyzerV0_1(
        StarlinkSymbolwiseReplayConfigV0_1(maximum_timing_search_cells=10),
        execution_context(),
    )
    with pytest.raises(ValueError, match="timing-search resource ceiling"):
        analyzer.resource_plan(
            sample_rate_hz=SAMPLE_RATE_HZ,
            segment_sample_count=WINDOW_SAMPLES,
            frequency_center=_frequency_center(),
        )


def test_bundle_validation_rejects_label_independent_geometry_drift() -> None:
    values = np.ones(WINDOW_SAMPLES, np.complex64)
    _, result = _analyze(values)

    with pytest.raises(ValueError, match="cadence"):
        replace(
            result,
            windows=(replace(result.windows[0], start_sample=1, stop_sample=25_001),),
        )
