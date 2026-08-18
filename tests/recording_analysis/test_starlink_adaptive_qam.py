# ruff: noqa: F401,F811 -- imported fixture is registered for this module.
from __future__ import annotations

from dataclasses import replace

from leo_flow.analysis.recording.starlink_adaptive_qam import (
    adaptive_qam_window_selections_v0_4,
)
from leo_flow.contracts.starlink_adaptive_qam import AdaptiveQamSelectionReason
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod

from .test_starlink_adaptive_response import adaptive_response_result


def test_adaptive_qam_selection_retains_target_margin_and_control_windows(
    adaptive_response_result,
) -> None:
    _view, _request, bundle = adaptive_response_result
    stream = bundle.streams[0]
    acquisition = tuple(
        point
        for point in stream.points
        if point.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
    )
    assert acquisition
    selected = adaptive_qam_window_selections_v0_4(
        stream, qam_window_sample_count=7_500, maximum_windows=3
    )

    assert selected
    assert tuple(item.qam_start_sample for item in selected) == tuple(
        sorted(item.qam_start_sample for item in selected)
    )
    reasons = {reason for item in selected for reason in item.reasons}
    assert {
        AdaptiveQamSelectionReason.QIN_SCORE,
        AdaptiveQamSelectionReason.QIN_MARGIN,
        AdaptiveQamSelectionReason.SURROGATE_SCORE,
    } <= reasons
    assert all(
        item.qam_stop_sample - item.qam_start_sample == 7_500 for item in selected
    )


def test_adaptive_qam_selection_is_label_independent_and_endpoint_safe(
    adaptive_response_result,
) -> None:
    _view, _request, bundle = adaptive_response_result
    stream = bundle.streams[0]
    renamed = replace(stream, lnb_id="lnb-swapped-after-legacy")

    original = adaptive_qam_window_selections_v0_4(
        stream, qam_window_sample_count=7_500, maximum_windows=3
    )
    swapped = adaptive_qam_window_selections_v0_4(
        renamed, qam_window_sample_count=7_500, maximum_windows=3
    )

    assert original == swapped
    assert all(0 <= item.qam_start_sample < item.qam_stop_sample for item in original)
    assert all(item.qam_stop_sample <= stream.segment_sample_count for item in original)
