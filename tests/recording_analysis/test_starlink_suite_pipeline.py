from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.adapters.dashboard_recording_postgres import (
    recording_starlink_suite_view_v0_2,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    run_starlink_injection_cases_v0_2,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
)
from leo_flow.analysis.recording.starlink_suite_codec import (
    MalformedStarlinkSuiteBundleError,
    decode_starlink_suite_bundle,
    encode_starlink_suite_bundle,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest, SchemaRef
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import REPORT_METHOD_ORDER, V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.contracts.storage import ObjectRef, PublishedRecordingRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobType
from leo_flow.services.starlink_suite_analysis import (
    decode_starlink_suite_analysis_payload,
)
from leo_flow.services.starlink_suite_submission import (
    StarlinkSuiteAnalysisSubmissionServiceV0_2,
    StarlinkSuiteAnalysisSubmissionV0_2,
)
from tests.recording_analysis.fakes import SegmentFixture, execution_context, make_view
from tests.recording_analysis.test_starlink_detector_suite import _bundle


def _recording_bundle(
    state: StarlinkSuiteRecordingState = StarlinkSuiteRecordingState.CANDIDATES,
):
    suite = _bundle()
    suites = (suite,) if state is StarlinkSuiteRecordingState.CANDIDATES else ()
    reasons = (
        ("whole-search-calibration-required",) if suites else ("clipped-pilot-band",)
    )
    bundle = StarlinkDetectorSuiteRecordingBundleV0_2(
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
        "slsuite_0123456789abcdef0123456789abcdef",
        suite.recording_id,
        suite.recording_identity_digest,
        state,
        suites,
        reasons,
        None,
    )
    payload = encode_starlink_suite_bundle(bundle)
    ref = StarlinkDetectorSuiteProductRefV0_2(
        bundle.analysis_id,
        bundle.recording_id,
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            "application/json",
            "starlink-detector-suite-recording-bundle-v0.2",
            f"cas:sha256:{Digest.sha256(payload).value}",
        ),
    )
    return bundle, ref


def test_v02_bundle_codec_and_dashboard_preserve_every_report_method() -> None:
    bundle, ref = _recording_bundle()
    encoded = encode_starlink_suite_bundle(bundle)
    assert decode_starlink_suite_bundle(encoded) == bundle
    view = recording_starlink_suite_view_v0_2(bundle, ref)
    assert view.state is StarlinkSuiteRecordingState.CANDIDATES
    assert tuple(item.method for item in view.methods) == REPORT_METHOD_ORDER
    assert view.method_count == 8
    assert view.calibrated_detection_count is None
    assert "whole-search-calibration-required" in view.reason_codes


def test_clipped_rate_is_explicit_terminal_not_evaluated() -> None:
    bundle, ref = _recording_bundle(StarlinkSuiteRecordingState.NOT_EVALUATED)
    assert decode_starlink_suite_bundle(encode_starlink_suite_bundle(bundle)) == bundle
    view = recording_starlink_suite_view_v0_2(bundle, ref)
    assert view.state is StarlinkSuiteRecordingState.NOT_EVALUATED
    assert view.analyzed_stream_count == view.method_count == 0
    assert view.reason_codes == ("clipped-pilot-band",)


def test_suite_codec_rejects_noncanonical_and_duplicate_json() -> None:
    bundle, _ = _recording_bundle()
    payload = encode_starlink_suite_bundle(bundle)
    with pytest.raises(MalformedStarlinkSuiteBundleError, match="canonical"):
        decode_starlink_suite_bundle(json.dumps(json.loads(payload), indent=2).encode())
    duplicate = payload.replace(
        b'{"analysis_id":', b'{"analysis_id":"duplicate","analysis_id":', 1
    )
    with pytest.raises(MalformedStarlinkSuiteBundleError, match="duplicate"):
        decode_starlink_suite_bundle(duplicate)


@pytest.mark.parametrize(
    ("sample_rate_hz", "expected_state"),
    ((1_250_000, "clipped-pilot-band"), (2_500_000, None), (5_000_000, None)),
)
def test_submission_has_terminal_clipped_state_and_exact_full_band_profiles(
    sample_rate_hz: int, expected_state: str | None
) -> None:
    view, recording = make_view(SegmentFixture(b"\0" * 8 * 4, sample_rate_hz))
    segment = view.manifest.segments[0]
    tagged_request = replace(
        segment.requested,
        tags=(
            ("edge", "lower"),
            ("pilot_band_fits", sample_rate_hz >= 1_875_000),
            ("scan_schema", "org.leo-flow.starlink-edge-scan/v1"),
        ),
    )
    manifest = replace(
        view.manifest, segments=(replace(segment, requested=tagged_request),)
    )
    config = StarlinkDetectorSuiteConfigV0_2((0,), (0.0,), (0.0,))
    jobs = InMemoryJobLeaseRepository()
    submitted = StarlinkSuiteAnalysisSubmissionServiceV0_2(jobs).submit(
        StarlinkSuiteAnalysisSubmissionV0_2(
            PublishedRecordingRef(recording),
            manifest,
            starlink_detector_suite_algorithm_ref_v0_2(),
            starlink_detector_suite_config_ref_v0_2(config),
            1,
        )
    )
    decoded = decode_starlink_suite_analysis_payload(submitted.payload)
    assert decoded.ineligible_reason == expected_state
    assert len(decoded.stream_selections) == (0 if expected_state else 2)
    assert jobs.claim((JobType.STARLINK_ANALYSIS,), "legacy", 5.0) is None
    lease = jobs.claim((JobType.STARLINK_SUITE_ANALYSIS,), "suite", 5.0)
    assert lease is not None and lease.job_id == submitted.job_id


def test_submission_accepts_additive_focused_monitor_geometry() -> None:
    view, recording = make_view(SegmentFixture(b"\0" * 8 * 4, 2_500_000))
    segment = view.manifest.segments[0]
    manifest = replace(
        view.manifest,
        segments=(
            replace(
                segment,
                requested=replace(
                    segment.requested,
                    tags=(
                        ("edge", "lower"),
                        ("pilot_band_fits", True),
                        (
                            "scan_schema",
                            "org.leo-flow.starlink-focused-monitor/v1",
                        ),
                    ),
                ),
            ),
        ),
    )
    jobs = InMemoryJobLeaseRepository()
    submitted = StarlinkSuiteAnalysisSubmissionServiceV0_2(jobs).submit(
        StarlinkSuiteAnalysisSubmissionV0_2(
            PublishedRecordingRef(recording),
            manifest,
            starlink_detector_suite_algorithm_ref_v0_2(),
            starlink_detector_suite_config_ref_v0_2(
                StarlinkDetectorSuiteConfigV0_2((0,), (0.0,), (0.0,))
            ),
            1,
        )
    )

    assert len(submitted.request.stream_selections) == 2


def test_positive_and_negative_cfo_and_fractional_frame_cadence_are_searched() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(2_500_000.0, StarlinkEdge.LOWER)
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (3,), (-1_000.0, 1_000.0), (-100.0, 0.0, 100.0)
        ),
        execution_context(),
    )
    cases = tuple(
        StarlinkInjectionCaseV0_2(
            f"cfo-{index}", 100 + index, 14_000, 2.0, 0.05, 3, cfo, 0.0, (0, 1, 2, 3)
        )
        for index, cfo in enumerate((-1_000.0, 1_000.0))
    )
    results = run_starlink_injection_cases_v0_2(analyzer, templates, cases)
    assert [result.bundle.methods[-3].winning_coarse_cfo_hz for result in results] == [
        -1_000.0,
        1_000.0,
    ]
    assert 2_500_000.0 / 750.0 != round(2_500_000.0 / 750.0)
    assert all(result.bundle.methods[-1].reported_score > 0.99 for result in results)


def test_seeded_snr_cfo_epoch_occupancy_drift_matrix_is_finite_bounded_and_complete() -> (
    None
):
    templates = qin_edge_pilot_template_pair_v0_1(2_500_000.0, StarlinkEdge.UPPER)
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (0, 3, 6), (-1_000.0, 0.0, 1_000.0), (-100.0, 0.0, 100.0)
        ),
        execution_context(),
    )
    cases = (
        StarlinkInjectionCaseV0_2(
            "matrix-a", 701, 14_000, 0.5, 0.5, 0, -1_000.0, -100.0, (0, 2)
        ),
        StarlinkInjectionCaseV0_2(
            "matrix-b", 702, 14_000, 1.0, 0.25, 3, 0.0, 0.0, (1, 2, 3)
        ),
        StarlinkInjectionCaseV0_2(
            "matrix-c", 703, 14_000, 2.0, 0.1, 6, 1_000.0, 100.0, (0, 1, 2, 3)
        ),
        StarlinkInjectionCaseV0_2(
            "matrix-null", 704, 14_000, 0.0, 0.5, 0, 0.0, 0.0, ()
        ),
    )
    results = run_starlink_injection_cases_v0_2(analyzer, templates, cases)
    assert tuple(result.case.case_id for result in results) == tuple(
        case.case_id for case in cases
    )
    for result in results:
        assert (
            tuple(item.method for item in result.bundle.methods) == REPORT_METHOD_ORDER
        )
        assert len(result.bundle.methods) == 8
        assert all(0.0 <= item.reported_score <= 1.0 for item in result.bundle.methods)
        assert all(
            item.effective_search_cell_count <= 1_000_000
            for item in result.bundle.methods
        )


def test_frame_period_translation_preserves_report_scores() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(2_500_000.0, StarlinkEdge.LOWER)
    period = round(2_500_000.0 / 750.0)
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2((3, 3 + period), (0.0,), (0.0,)),
        execution_context(),
    )
    cases = (
        StarlinkInjectionCaseV0_2(
            "translate-a", 801, 14_000, 2.0, 0.0, 3, 0.0, 0.0, (0, 1, 2, 3)
        ),
        StarlinkInjectionCaseV0_2(
            "translate-b",
            801,
            14_000 + period,
            2.0,
            0.0,
            3 + period,
            0.0,
            0.0,
            (0, 1, 2, 3),
        ),
    )
    first, translated = run_starlink_injection_cases_v0_2(analyzer, templates, cases)
    assert [item.reported_score for item in translated.bundle.methods] == pytest.approx(
        [item.reported_score for item in first.bundle.methods], abs=1e-12
    )
