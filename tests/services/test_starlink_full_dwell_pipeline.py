from __future__ import annotations

from dataclasses import replace

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
)
from leo_flow.analysis.recording.starlink_suite_codec import (
    encode_starlink_suite_bundle,
)
from leo_flow.contracts.core import Digest, SchemaRef
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.contracts.storage import ObjectRef, PublishedRecordingRef
from leo_flow.services.starlink_full_dwell_pipeline import full_dwell_request_v0_1
from leo_flow.services.starlink_full_dwell_producer import FullDwellWorkLeaseV0_1
from tests.recording_analysis.fakes import SegmentFixture, make_view
from tests.recording_analysis.test_starlink_detector_suite import _bundle


def test_request_is_derived_from_exact_suite_and_recording_geometry() -> None:
    view, recording_ref = make_view(SegmentFixture(b"\0" * 8 * 20_000, 2_500_000))
    segment = view.manifest.segments[0]
    source = _bundle()
    suite = replace(
        source,
        recording_id=recording_ref.recording_id,
        recording_identity_digest=recording_ref.identity_digest(),
        segment_id=segment.segment_id,
        receiver_chain_id=segment.requested.receiver_chain_ids[0],
    )
    bundle = StarlinkDetectorSuiteRecordingBundleV0_2(
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
        "slsuite_" + "4" * 32,
        recording_ref.recording_id,
        recording_ref.identity_digest(),
        StarlinkSuiteRecordingState.CANDIDATES,
        (suite,),
        ("whole-search-calibration-required",),
        None,
    )
    payload = encode_starlink_suite_bundle(bundle)
    suite_ref = StarlinkDetectorSuiteProductRefV0_2(
        bundle.analysis_id,
        bundle.recording_id,
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            "application/json",
            "starlink-detector-suite-recording-bundle-v0.2",
            "cas:suite",
        ),
    )
    lease = FullDwellWorkLeaseV0_1(
        suite_ref, Digest.sha256(b"suite-request"), "lease", 1, 1
    )
    config = StarlinkDetectorSuiteConfigV0_2(
        (0, 3, 6), (0.0, 1_000.0), (-100.0, 0.0, 100.0)
    )
    request = full_dwell_request_v0_1(
        PublishedRecordingRef(recording_ref), view, bundle, lease, config
    )
    assert request.source_suite_ref.digest == suite_ref.bundle_ref.digest
    assert request.source_suite_request_digest == lease.source_suite_request_digest
    assert request.plan.coarse_window_sample_count == 20_000
    assert request.plan.maximum_fine_window_count == 32
    assert request.plan.surrogate_count == 4
    assert request.stream_selections[0].segment_sample_count == segment.sample_count
