"""Exact source-closed production of a full-dwell response from a suite lease."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    starlink_detector_suite_config_ref_v0_2,
)
from leo_flow.analysis.recording.starlink_full_dwell_response import (
    ExactStarlinkFullDwellResponseAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
    DurableStarlinkFullDwellStoreV0_1,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    DurableStarlinkSuiteStoreV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, SchemaRef
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_full_dwell_response import (
    V0_1,
    StarlinkFullDwellPlanV0_1,
    StarlinkFullDwellRequestV0_1,
    StarlinkFullDwellResponseBundleV0_1,
    StarlinkFullDwellStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog
from leo_flow.storage.ports import RecordingObjectReader, RecordingView

from .starlink_full_dwell_producer import FullDwellWorkLeaseV0_1


@dataclass(frozen=True)
class FullDwellAnalysisProfileV0_1:
    config: StarlinkDetectorSuiteConfigV0_2
    execution: AnalysisExecutionContext


class DurableFullDwellLeaseProducerV0_1:
    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        reader: RecordingObjectReader,
        suites: DurableStarlinkSuiteStoreV0_2,
        products: DurableStarlinkFullDwellStoreV0_1,
        profiles: tuple[FullDwellAnalysisProfileV0_1, ...],
    ) -> None:
        refs = tuple(starlink_detector_suite_config_ref_v0_2(p.config) for p in profiles)
        if not profiles or len(refs) != len(set(refs)):
            raise ValueError("full-dwell profiles must be nonempty and unique")
        self._recordings = recordings
        self._reader = reader
        self._suites = suites
        self._products = products
        self._profiles = profiles

    def produce(self, lease: FullDwellWorkLeaseV0_1) -> ArtifactRef:
        published = self._recordings.get(lease.source_suite_ref.recording_id)
        if published is None:
            raise ValueError("full-dwell recording is not published")
        with (
            self._suites.open(lease.source_suite_ref) as suite_bundle,
            self._reader.open(published.recording_object) as recording,
        ):
            profile = self._profile(suite_bundle)
            request = full_dwell_request_v0_1(
                published,
                recording,
                suite_bundle,
                lease,
                profile.config,
            )
            bundle = ExactStarlinkFullDwellResponseAnalyzerV0_1(
                profile.config, profile.execution
            ).analyze_full_dwell(recording, request)
        ref = self._products.publish(
            request,
            bundle,
            idempotency_key=(
                f"full-dwell:{lease.source_suite_ref.analysis_id}:{request.digest.value}"
            ),
        )
        return ArtifactRef(ref.analysis_id, ref.bundle_ref.digest, bundle.schema)

    def _profile(
        self, bundle: StarlinkDetectorSuiteRecordingBundleV0_2
    ) -> FullDwellAnalysisProfileV0_1:
        if not bundle.suites:
            raise ValueError("full-dwell source suite has no eligible streams")
        refs = {method.config_ref for suite in bundle.suites for method in suite.methods}
        matches = tuple(
            profile
            for profile in self._profiles
            if refs == {starlink_detector_suite_config_ref_v0_2(profile.config)}
        )
        if len(matches) != 1:
            raise ValueError("full-dwell source has no unique approved profile")
        return matches[0]


def full_dwell_request_v0_1(
    published: PublishedRecordingRef,
    recording: RecordingView,
    suite_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    lease: FullDwellWorkLeaseV0_1,
    config: StarlinkDetectorSuiteConfigV0_2,
) -> StarlinkFullDwellRequestV0_1:
    if (
        suite_bundle.analysis_id != lease.source_suite_ref.analysis_id
        or suite_bundle.recording_id != published.recording_id
        or suite_bundle.recording_identity_digest
        != published.recording_object.identity_digest()
    ):
        raise ValueError("full-dwell source identities differ")
    segments = {segment.segment_id: segment for segment in recording.manifest.segments}
    selections = []
    for suite in suite_bundle.suites:
        segment = segments.get(suite.segment_id)
        if segment is None or suite.receiver_chain_id not in segment.requested.receiver_chain_ids:
            raise ValueError("full-dwell source stream is unavailable")
        if suite.sample_rate_hz != segment.actual_sample_rate_hz:
            raise ValueError("full-dwell source sample rate differs")
        selections.append(
            StarlinkFullDwellStreamSelectionV0_1(
                suite.segment_id,
                suite.receiver_chain_id,
                suite.edge,
                suite.sample_rate_hz,
                segment.sample_count,
            )
        )
    selections.sort(key=lambda item: tuple(map(str, (item.segment_id, item.receiver_chain_id, item.edge))))
    window_samples = round(suite_bundle.suites[0].sample_rate_hz * 0.008)
    plan = StarlinkFullDwellPlanV0_1(
        window_samples, window_samples, window_samples, 16_384, 32, 4
    )
    return StarlinkFullDwellRequestV0_1(
        SchemaRef(StarlinkFullDwellRequestV0_1.SCHEMA_ID, V0_1),
        published.recording_id,
        published.recording_object,
        ArtifactRef(
            suite_bundle.analysis_id,
            lease.source_suite_ref.bundle_ref.digest,
            SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
        ),
        lease.source_suite_request_digest,
        starlink_search_grid_v0_1(config),
        plan,
        tuple(selections),
        SchemaRef(StarlinkFullDwellResponseBundleV0_1.SCHEMA_ID, V0_1),
    )
