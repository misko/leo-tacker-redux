from __future__ import annotations

import struct
from typing import cast

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_full_search_control_recording import (
    ExactStarlinkFullSearchControlRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import ReceiverChainId, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_full_search_control import (
    StarlinkFullSearchControlRecordingState,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteStreamSelectionV0_2,
)
from leo_flow.storage.ports import RecordingView

from .fakes import SegmentFixture, execution_context, make_view

SAMPLE_RATE_HZ = 2_500_000


def _ci16_pair(values: tuple[complex, ...]) -> bytes:
    def quantize(value: float) -> int:
        return max(-32_768, min(32_767, round(value * 1_000)))

    return b"".join(
        struct.pack(
            "<hhhh",
            quantize(value.real),
            quantize(value.imag),
            quantize(value.real),
            quantize(value.imag),
        )
        for value in values
    )


def _suite() -> tuple[StarlinkDetectorSuiteV0_2, StarlinkDetectorSuiteConfigV0_2]:
    config = StarlinkDetectorSuiteConfigV0_2(
        (0, 3, 6),
        (0.0, 1_000.0),
        (-100.0, 0.0, 100.0),
    )
    return StarlinkDetectorSuiteV0_2(config, execution_context()), config


def test_recording_bridge_reads_exact_requested_stream_and_is_deterministic() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "recording-control",
            13,
            14_000,
            2.0,
            0.1,
            3,
            1_000.0,
            0.0,
            (0, 1, 2, 3),
        ),
    )
    view, recording_ref = make_view(SegmentFixture(_ci16_pair(values), SAMPLE_RATE_HZ))
    suite, config = _suite()
    request = StarlinkDetectorSuiteRequestV0_2(
        SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
        recording_ref.recording_id,
        recording_ref,
        starlink_detector_suite_algorithm_ref_v0_2(),
        starlink_detector_suite_config_ref_v0_2(config),
        (
            StarlinkSuiteStreamSelectionV0_2(
                view.manifest.segments[0].segment_id,
                ReceiverChainId("rx_0"),
                StarlinkEdge.LOWER,
                templates.exact_ref,
                templates.conditioned_control_ref,
                len(values),
            ),
        ),
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
    )
    analyzer = ExactStarlinkFullSearchControlRecordingAnalyzerV0_1(suite)

    first = analyzer.analyze_full_search_controls(cast(RecordingView, view), request)
    second = analyzer.analyze_full_search_controls(cast(RecordingView, view), request)

    assert first == second
    assert first.state is StarlinkFullSearchControlRecordingState.CANDIDATES
    assert len(first.suites) == 1
    assert len(first.suites[0].methods) == 8
    assert view.calls == [
        (view.manifest.segments[0].segment_id, 0, len(values)),
        (view.manifest.segments[0].segment_id, 0, len(values)),
    ]


def test_recording_bridge_preserves_clipped_not_evaluated_semantics() -> None:
    view, recording_ref = make_view(SegmentFixture(b"\0" * 80, 1_250_000))
    suite, config = _suite()
    request = StarlinkDetectorSuiteRequestV0_2(
        SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
        recording_ref.recording_id,
        recording_ref,
        starlink_detector_suite_algorithm_ref_v0_2(),
        starlink_detector_suite_config_ref_v0_2(config),
        (),
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
        "clipped-pilot-band",
    )

    result = ExactStarlinkFullSearchControlRecordingAnalyzerV0_1(
        suite
    ).analyze_full_search_controls(cast(RecordingView, view), request)

    assert result.state is StarlinkFullSearchControlRecordingState.NOT_EVALUATED
    assert result.suites == ()
    assert result.reason_codes == ("clipped-pilot-band",)
    assert view.calls == []
