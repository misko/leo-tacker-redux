from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_refinement import (
    ExactStarlinkPilotRefinementAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_pilot_refinement_codec import (
    MalformedStarlinkPilotRefinementError,
    decode_starlink_pilot_refinement,
    encode_starlink_pilot_refinement,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementBundleV0_1,
    StarlinkPilotRefinementRequestV0_1,
    StarlinkPilotRefinementSeedV0_1,
    StarlinkPilotRefinementStreamSelectionV0_1,
)
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view


def _paired_ci16(left: tuple[complex, ...], right: tuple[complex, ...]) -> bytes:
    output = bytearray()
    for first, second in zip(left, right, strict=True):
        for value in (first, second):
            output.extend(round(value.real * 512).to_bytes(2, "little", signed=True))
            output.extend(round(value.imag * 512).to_bytes(2, "little", signed=True))
    return bytes(output)


def test_prescreen_refinement_runs_all_methods_and_symmetric_controls() -> None:
    rate = 2_500_000.0
    config = StarlinkDetectorSuiteConfigV0_2((0, 3), (0.0, 1_000.0), (0.0,))
    template = qin_edge_pilot_template_pair_v0_1(rate, StarlinkEdge.LOWER)
    samples = synthesize_starlink_injection_v0_2(
        template,
        StarlinkInjectionCaseV0_2(
            "prescreen-seed", 51, 20_000, 1.1, 0.03, 3, 1_000.0, 0.0, (0, 1)
        ),
    )
    data = _paired_ci16(samples, samples)
    original, recording_ref = make_view(SegmentFixture(data, int(rate)))
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
    request = StarlinkPilotRefinementRequestV0_1(
        SchemaRef(StarlinkPilotRefinementRequestV0_1.SCHEMA_ID, V0_1),
        recording_ref.recording_id,
        recording_ref,
        ArtifactRef("prescreen", Digest.sha256(b"prescreen"), None),
        ArtifactRef("suite", Digest.sha256(b"suite"), None),
        Digest.sha256(b"suite-request"),
        starlink_search_grid_v0_1(config),
        (
            StarlinkPilotRefinementStreamSelectionV0_1(
                RadioId("radio_synthetic"),
                "lnb-current",
                segment.segment_id,
                ReceiverChainId("rx_0"),
                4,
                StarlinkEdge.LOWER,
                rate,
                segment.sample_count,
                (StarlinkPilotRefinementSeedV0_1(0, 0, 20_000, 0.8, 100.0, 0, None),),
            ),
        ),
        4,
        SchemaRef(StarlinkPilotRefinementBundleV0_1.SCHEMA_ID, V0_1),
    )
    result = ExactStarlinkPilotRefinementAnalyzerV0_1(
        config, execution_context()
    ).analyze(cast(RecordingView, view), request)

    stream = result.streams[0]
    assert tuple(point.method for point in stream.points) == REPORT_METHOD_ORDER
    assert all(len(point.surrogates) == 4 for point in stream.points)
    assert stream.selection.seeds[0].reasons == ("top-ofdm-periodicity",)
    assert stream.exact_coverage_fraction == 1.0
    assert result.calibrated_detection_count is None
    payload = encode_starlink_pilot_refinement(result)
    assert decode_starlink_pilot_refinement(payload) == result
    with pytest.raises(MalformedStarlinkPilotRefinementError, match="canonical"):
        decode_starlink_pilot_refinement(payload + b"\n")
