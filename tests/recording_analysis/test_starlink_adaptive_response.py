from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_adaptive_response import (
    ExactStarlinkAdaptiveResponseAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_adaptive_refinement import (
    StarlinkAdaptiveRefinementPlanV0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    V0_1,
    StarlinkAdaptivePowerSeedV0_1,
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponseRequestV0_1,
    StarlinkAdaptiveStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER
from leo_flow.contracts.starlink_surrogate_null import StarlinkPatternSearchMode
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view

SAMPLE_RATE_HZ = 2_500_000.0


def _paired_ci16(rx0: tuple[complex, ...], rx1: tuple[complex, ...]) -> bytes:
    result = bytearray()
    for left, right in zip(rx0, rx1, strict=True):
        for value in (left, right):
            i = round(max(-2048, min(2047, value.real * 512)))
            q = round(max(-2048, min(2047, value.imag * 512)))
            result.extend(int(i).to_bytes(2, "little", signed=True))
            result.extend(int(q).to_bytes(2, "little", signed=True))
    return bytes(result)


@pytest.fixture(scope="module")
def adaptive_response_result():
    config = StarlinkDetectorSuiteConfigV0_2((0, 3), (0.0, 1_000.0), (0.0,))
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    injection = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "adaptive-middle", 41, 7_500, 1.2, 0.04, 3, 1_000.0, 0.0, (0, 1)
        ),
    )
    zero = tuple(0j for _ in injection)
    data = _paired_ci16(zero + injection + zero, zero + injection + zero)
    original, recording_ref = make_view(SegmentFixture(data, int(SAMPLE_RATE_HZ)))
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested, tags=(("channel", "4"), ("edge", "lower"))
        ),
    )
    view = FakeRecordingView(
        replace(original.manifest, segments=(tagged,)), {segment.segment_id: data}
    )
    plan = StarlinkAdaptiveRefinementPlanV0_1(
        7_500,
        15_000,
        7_500,
        3_750,
        1,
        1,
        8,
        32,
    )
    request = StarlinkAdaptiveResponseRequestV0_1(
        SchemaRef(StarlinkAdaptiveResponseRequestV0_1.SCHEMA_ID, V0_1),
        recording_ref.recording_id,
        recording_ref,
        ArtifactRef("timeline", Digest.sha256(b"timeline"), None),
        Digest.sha256(b"timeline-request"),
        ArtifactRef("suite", Digest.sha256(b"suite"), None),
        Digest.sha256(b"suite-request"),
        starlink_search_grid_v0_1(config),
        plan,
        (
            StarlinkAdaptiveStreamSelectionV0_1(
                RadioId("radio_synthetic"),
                "lnb-current-a",
                segment.segment_id,
                ReceiverChainId("rx_0"),
                4,
                StarlinkEdge.LOWER,
                SAMPLE_RATE_HZ,
                segment.sample_count,
                (StarlinkAdaptivePowerSeedV0_1(0, 7_500, 15_000),),
            ),
        ),
        4,
        SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
    )
    result = ExactStarlinkAdaptiveResponseAnalyzerV0_1(
        config, execution_context()
    ).analyze(cast(RecordingView, view), request)
    return view, request, result


def test_adaptive_response_runs_every_method_on_pattern_symmetric_union(
    adaptive_response_result,
) -> None:
    _view, _request, result = adaptive_response_result
    stream = result.streams[0]
    assert tuple((item.window_index, item.method) for item in stream.points) == tuple(
        (window.window_index, method)
        for window in stream.selection.exact_windows
        for method in REPORT_METHOD_ORDER
    )
    assert stream.selection.exact_windows[0].start_sample == 0
    assert stream.selection.exact_windows[-1].stop_sample == stream.segment_sample_count
    assert all(len(item.surrogates) == 4 for item in stream.points)
    assert all(item.qin.effective_search_cell_count > 0 for item in stream.points)
    for window in stream.selection.exact_windows:
        points = tuple(
            item for item in stream.points if item.window_index == window.window_index
        )
        assert (
            sum(
                item.qin.search_mode is StarlinkPatternSearchMode.SEARCHED
                for item in points
            )
            == 1
        )
        assert all(
            sum(
                surrogate.winner.search_mode is StarlinkPatternSearchMode.SEARCHED
                for surrogate in pattern_results
            )
            == 1
            for pattern_results in zip(
                *(item.surrogates for item in points), strict=True
            )
        )
    assert stream.exact_coverage_fraction > 0
    assert result.calibrated_detection_count is None
    assert "time-look-elsewhere-calibration-required" in result.warnings
