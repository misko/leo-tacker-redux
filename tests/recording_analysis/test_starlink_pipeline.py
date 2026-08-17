from __future__ import annotations

import json
import struct
from dataclasses import replace

import pytest

from leo_flow.adapters.dashboard_recording_postgres import (
    recording_starlink_candidate_view_v0_1,
)
from leo_flow.analysis.recording.starlink import (
    KnownCodePilotSearchConfigV0_1,
    KnownCodePilotSearchV0_1,
    known_code_pilot_algorithm_ref_v0_1,
    known_code_pilot_config_ref_v0_1,
)
from leo_flow.analysis.recording.starlink_codec import (
    MalformedStarlinkBundleError,
    decode_starlink_bundle,
    encode_starlink_bundle,
)
from leo_flow.analysis.recording.starlink_persistence import starlink_projection_v0_1
from leo_flow.analysis.recording.starlink_recording import (
    ExactKnownCodeRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import SchemaRef
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkPilotAnalysisBundleV0_1,
    StarlinkRecordingDecisionState,
)
from leo_flow.contracts.starlink_pipeline import (
    StarlinkPilotAnalysisProductRefV0_1,
    StarlinkPilotAnalysisRequestV0_1,
    StarlinkStreamSelectionV0_1,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobType
from leo_flow.services.starlink_analysis import (
    decode_starlink_analysis_payload,
    starlink_analysis_payload,
)
from leo_flow.services.starlink_submission import (
    StarlinkAnalysisSubmissionServiceV0_1,
    StarlinkAnalysisSubmissionV0_1,
    select_qin_starlink_streams_v0_1,
)
from tests.recording_analysis.fakes import (
    RX_IDS,
    SegmentFixture,
    execution_context,
    make_view,
)


def _templates():
    return qin_edge_pilot_template_pair_v0_1(12_000.0, StarlinkEdge.LOWER)


def _fixture():
    templates = _templates()
    values = [0j] * 82
    for frame in range(5):
        start = 2 + frame * 16
        for index, sample in enumerate(templates.exact_samples):
            values[start + index] = sample
    raw = b"".join(
        struct.pack("<hhhh", round(sample.real), round(sample.imag), 0, 0)
        for sample in values
    )
    view, recording = make_view(SegmentFixture(raw, 12_000))
    config = KnownCodePilotSearchConfigV0_1((0, 1, 2, 3), (0.0,))
    algorithm_ref = known_code_pilot_algorithm_ref_v0_1()
    config_ref = known_code_pilot_config_ref_v0_1(config)
    selection = StarlinkStreamSelectionV0_1(
        view.manifest.segments[0].segment_id,
        RX_IDS[0],
        StarlinkEdge.LOWER,
        templates.exact_ref,
        templates.conditioned_control_ref,
        len(values),
    )
    request = StarlinkPilotAnalysisRequestV0_1(
        SchemaRef(StarlinkPilotAnalysisRequestV0_1.SCHEMA_ID),
        recording.recording_id,
        recording,
        algorithm_ref,
        config_ref,
        (selection,),
        SchemaRef(StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID),
    )
    analyzer = ExactKnownCodeRecordingAnalyzerV0_1(
        KnownCodePilotSearchV0_1(config, execution_context()),
        algorithm_ref=algorithm_ref,
        config_ref=config_ref,
    )
    bundle = analyzer.analyze_starlink(view, request)
    return request, bundle


def test_exact_recording_pipeline_payload_codec_and_projection_round_trip() -> None:
    request, bundle = _fixture()
    assert (
        decode_starlink_analysis_payload(starlink_analysis_payload(request)) == request
    )
    encoded = encode_starlink_bundle(bundle)
    assert decode_starlink_bundle(encoded) == bundle
    projection = starlink_projection_v0_1(request, bundle)
    assert projection.candidate_count == projection.analyzed_stream_count == 1
    assert bundle.candidates[0].winning_epoch_sample == 2
    assert bundle.candidates[0].probe_sample_count == 82

    noncanonical = json.dumps(json.loads(encoded), indent=2).encode()
    with pytest.raises(MalformedStarlinkBundleError, match="canonical"):
        decode_starlink_bundle(noncanonical)


def test_submission_is_content_addressed_and_claims_only_starlink_jobs() -> None:
    request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository()
    service = StarlinkAnalysisSubmissionServiceV0_1(jobs)
    submission = StarlinkAnalysisSubmissionV0_1(
        PublishedRecordingRef(request.recording_object_ref),
        request.algorithm_ref,
        request.config_ref,
        request.stream_selections,
    )
    first = service.submit(submission)
    second = service.submit(submission)
    assert first == second
    assert jobs.claim((JobType.RECORDING_ANALYSIS,), "wrong", 5.0) is None
    lease = jobs.claim((JobType.STARLINK_ANALYSIS,), "starlink", 5.0)
    assert lease is not None and lease.job_id == first.job_id


def test_submission_job_identity_includes_exact_rate_profile() -> None:
    request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository()
    service = StarlinkAnalysisSubmissionServiceV0_1(jobs)
    jobs_by_rate = []
    for rate, probe, stride in (
        (2_500_000.0, 20_000, 64),
        (5_000_000.0, 40_000, 128),
    ):
        templates = qin_edge_pilot_template_pair_v0_1(rate, StarlinkEdge.LOWER)
        config = KnownCodePilotSearchConfigV0_1(
            tuple(range(0, round(rate / 750), stride)),
            tuple(float(value) for value in range(-100_000, 100_001, 20_000)),
            maximum_search_cells=1_024,
            maximum_probe_samples=probe,
        )
        selection = replace(
            request.stream_selections[0],
            exact_template_ref=templates.exact_ref,
            conditioned_control_template_ref=templates.conditioned_control_ref,
            probe_sample_count=probe,
        )
        jobs_by_rate.append(
            service.submit(
                StarlinkAnalysisSubmissionV0_1(
                    PublishedRecordingRef(request.recording_object_ref),
                    request.algorithm_ref,
                    known_code_pilot_config_ref_v0_1(config),
                    (selection,),
                )
            ).job_id
        )

    assert jobs_by_rate[0] != jobs_by_rate[1]


def _tagged_edge_manifest(
    sample_rate_hz: int, sample_count: int, *, pilot_band_fits: bool = True
):
    view, _ = make_view(SegmentFixture(b"\0" * 8 * sample_count, sample_rate_hz))
    segment = view.manifest.segments[0]
    tagged_request = replace(
        segment.requested,
        tags=(
            ("edge", "lower"),
            ("pilot_band_fits", pilot_band_fits),
            ("scan_schema", "org.leo-flow.starlink-edge-scan/v1"),
        ),
    )
    return replace(
        view.manifest,
        segments=(replace(segment, requested=tagged_request),),
    )


@pytest.mark.parametrize(
    ("sample_rate_hz", "probe_sample_count"),
    ((2_500_000, 20_000), (5_000_000, 40_000)),
)
def test_qin_selection_uses_exact_full_pilot_rate_profile(
    sample_rate_hz: int, probe_sample_count: int
) -> None:
    manifest = _tagged_edge_manifest(sample_rate_hz, probe_sample_count)
    selected = select_qin_starlink_streams_v0_1(
        manifest,
        sample_rate_hz=float(sample_rate_hz),
        probe_sample_count=probe_sample_count,
    )
    expected = qin_edge_pilot_template_pair_v0_1(
        float(sample_rate_hz), StarlinkEdge.LOWER
    )
    assert len(selected) == len(manifest.segments[0].requested.receiver_chain_ids)
    assert selected[0].probe_sample_count == probe_sample_count
    assert selected[0].exact_template_ref == expected.exact_ref
    assert selected[0].conditioned_control_template_ref == (
        expected.conditioned_control_ref
    )


def test_qin_selection_rejects_cross_rate_and_clipped_pilot() -> None:
    manifest = _tagged_edge_manifest(2_500_000, 20_000)
    with pytest.raises(ValueError, match="sample rate"):
        select_qin_starlink_streams_v0_1(
            manifest, sample_rate_hz=5_000_000.0, probe_sample_count=20_000
        )
    clipped = _tagged_edge_manifest(1_250_000, 10_000, pilot_band_fits=False)
    with pytest.raises(ValueError, match="clipped-pilot"):
        select_qin_starlink_streams_v0_1(
            clipped, sample_rate_hz=1_250_000.0, probe_sample_count=10_000
        )


def test_dashboard_candidate_projection_never_manufactures_detection_count() -> None:
    _, bundle = _fixture()
    payload = encode_starlink_bundle(bundle)
    from leo_flow.contracts.core import Digest
    from leo_flow.contracts.storage import ObjectRef

    bundle_ref = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        "application/json",
        "starlink-pilot-analysis-bundle-v0.1",
        "memory://starlink",
    )
    view = recording_starlink_candidate_view_v0_1(
        bundle,
        StarlinkPilotAnalysisProductRefV0_1(
            bundle.analysis_id, bundle.recording_id, bundle_ref
        ),
    )
    assert view.decision.state is StarlinkRecordingDecisionState.CANDIDATES
    assert view.decision.calibrated_detection_count is None
    assert view.candidates[0].exact_minus_control_margin == pytest.approx(
        bundle.candidates[0].exact_minus_control_margin
    )
