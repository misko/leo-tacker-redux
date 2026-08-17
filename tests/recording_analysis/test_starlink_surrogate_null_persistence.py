from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_suite_recording import (
    ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    CatalogedStarlinkSurrogateNullV0_1,
    DurableRecordingStarlinkSurrogateNullQueryV0_1,
    DurableStarlinkSurrogateNullStoreV0_1,
    StarlinkSurrogateNullConflictError,
    StarlinkSurrogateNullIntegrityError,
    starlink_surrogate_null_projection_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_recording import (
    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_recording_codec import (
    MalformedStarlinkSurrogateNullRecordingError,
    decode_starlink_surrogate_null_recording,
    encode_starlink_surrogate_null_recording,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.analysis.recording.starlink_temporal_pilot_recording import (
    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
)
from leo_flow.contracts.core import (
    JobId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    V0_2,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
    StarlinkSuiteStreamSelectionV0_2,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullCatalogProjectionV0_1,
    StarlinkSurrogateNullProductRefV0_1,
    StarlinkSurrogateNullQueryV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.services.starlink_suite_analysis import starlink_suite_analysis_payload
from leo_flow.services.starlink_suite_surrogate_analysis import (
    CombinedStarlinkSuiteAnalysisJobPreparerV0_2,
)
from leo_flow.services.starlink_surrogate_null_analysis import (
    DurableStarlinkSurrogateNullCommitterV0_1,
    PreparedStarlinkSurrogateNullAnalysisV0_1,
    StarlinkSurrogateNullAnalysisPreparerV0_1,
    starlink_surrogate_null_request_v0_1,
)
from leo_flow.services.starlink_temporal_pilot_analysis import (
    StarlinkTemporalPilotAnalysisPreparerV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.ports import RecordingView

from .fakes import FakeRecordingView, SegmentFixture, execution_context, make_view

SAMPLE_RATE_HZ = 2_500_000


def _config() -> StarlinkDetectorSuiteConfigV0_2:
    return StarlinkDetectorSuiteConfigV0_2(
        (0, 3),
        (0.0, 1_000.0),
        (0.0, 50.0),
    )


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


def _fixture() -> tuple[
    FakeRecordingView,
    RecordingObjectRef,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteConfigV0_2,
]:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "surrogate-persistence",
            77,
            7_500,
            1.5,
            0.1,
            3,
            1_000.0,
            0.0,
            (0, 1),
        ),
    )
    data = _ci16_pair(values)
    original, recording_ref = make_view(SegmentFixture(data, SAMPLE_RATE_HZ))
    segment = original.manifest.segments[0]
    tagged = replace(
        segment,
        requested=replace(
            segment.requested,
            tags=(("channel", "4"), ("edge", "lower")),
        ),
    )
    manifest = replace(original.manifest, segments=(tagged,))
    view = FakeRecordingView(manifest, {segment.segment_id: data})
    config = _config()
    source_request = StarlinkDetectorSuiteRequestV0_2(
        SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
        recording_ref.recording_id,
        recording_ref,
        starlink_detector_suite_algorithm_ref_v0_2(),
        starlink_detector_suite_config_ref_v0_2(config),
        (
            StarlinkSuiteStreamSelectionV0_2(
                segment.segment_id,
                ReceiverChainId("rx_0"),
                StarlinkEdge.LOWER,
                templates.exact_ref,
                templates.conditioned_control_ref,
                len(values),
            ),
        ),
        SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
    )
    source_bundle = ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
        StarlinkDetectorSuiteV0_2(config, execution_context())
    ).analyze_starlink_suite(cast(RecordingView, view), source_request)
    return view, recording_ref, source_request, source_bundle, config


PreparedFixture = tuple[
    FakeRecordingView,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    PreparedStarlinkSurrogateNullAnalysisV0_1,
]


@pytest.fixture(scope="module")
def prepared() -> PreparedFixture:
    view, _, source_request, source_bundle, config = _fixture()
    request = starlink_surrogate_null_request_v0_1(
        source_request,
        source_bundle,
        starlink_search_grid_v0_1(config),
    )
    bundle = ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
        config, execution_context()
    ).analyze_surrogate_null(cast(RecordingView, view), request)
    return (
        view,
        source_request,
        source_bundle,
        PreparedStarlinkSurrogateNullAnalysisV0_1(request, bundle),
    )


class _Reader:
    def __init__(self, view: FakeRecordingView) -> None:
        self._view = view
        self.open_count = 0

    @contextmanager
    def open(self, recording_ref: RecordingObjectRef) -> Iterator[RecordingView]:
        if recording_ref.recording_id != self._view.manifest.recording_id:
            raise LookupError(recording_ref.recording_id)
        self.open_count += 1
        yield cast(RecordingView, self._view)


def test_combined_preparer_uses_exact_profile_and_one_recording_open() -> None:
    view, _, source_request, _, config = _fixture()
    reader = _Reader(view)
    suite_analyzer = ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
        StarlinkDetectorSuiteV0_2(config, execution_context())
    )
    surrogate = StarlinkSurrogateNullAnalysisPreparerV0_1(
        reader,
        ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(config, execution_context()),
        starlink_search_grid_v0_1(config),
    )
    preparer = CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
        reader,
        suite_analyzer,
        ((source_request.config_ref, surrogate),),
        StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(), execution_context()
        ),
        (
            (
                source_request.config_ref,
                StarlinkTemporalPilotAnalysisPreparerV0_1(
                    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1(
                        config, execution_context()
                    ),
                    starlink_search_grid_v0_1(config),
                ),
            ),
        ),
    )
    lease = JobLease(
        JobId("job_surrogate_combined"),
        JobType.STARLINK_SUITE_ANALYSIS,
        starlink_suite_analysis_payload(source_request),
        1,
        "surrogate-combined-token",
        1,
        UtcNs(100),
    )

    result = preparer.prepare(lease)

    assert result.request == source_request
    assert result.surrogate_null.request.source_suite_ref.artifact_id == (
        result.bundle.analysis_id
    )
    assert result.surrogate_null.bundle.source_suite_ref.digest == result.bundle.digest
    assert result.surrogate_null.bundle.streams
    assert result.pilot_constellation is not None
    assert result.pilot_constellation.bundle.streams
    assert result.temporal_pilot is not None
    assert result.temporal_pilot.bundle.streams
    assert reader.open_count == 1


def test_combined_preparer_rejects_unregistered_suite_profile() -> None:
    view, _, source_request, _, config = _fixture()
    reader = _Reader(view)
    preparer = CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
        reader,
        ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
            StarlinkDetectorSuiteV0_2(config, execution_context())
        ),
        (
            (
                replace(source_request.config_ref, artifact_id="another-config"),
                StarlinkSurrogateNullAnalysisPreparerV0_1(
                    reader,
                    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
                        config, execution_context()
                    ),
                    starlink_search_grid_v0_1(config),
                ),
            ),
        ),
        StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(), execution_context()
        ),
    )
    lease = JobLease(
        JobId("job_surrogate_wrong_profile"),
        JobType.STARLINK_SUITE_ANALYSIS,
        starlink_suite_analysis_payload(source_request),
        1,
        "surrogate-wrong-profile-token",
        1,
        UtcNs(100),
    )

    with pytest.raises(ValueError, match="no exact surrogate profile"):
        preparer.prepare(lease)


def test_combined_preparer_omits_qam_for_explicit_ineligible_suite() -> None:
    view, _, eligible, _, config = _fixture()
    request = replace(
        eligible,
        stream_selections=(),
        ineligible_reason="clipped-pilot-band",
    )
    reader = _Reader(view)
    preparer = CombinedStarlinkSuiteAnalysisJobPreparerV0_2(
        reader,
        ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2(
            StarlinkDetectorSuiteV0_2(config, execution_context())
        ),
        (
            (
                request.config_ref,
                StarlinkSurrogateNullAnalysisPreparerV0_1(
                    reader,
                    ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
                        config, execution_context()
                    ),
                    starlink_search_grid_v0_1(config),
                ),
            ),
        ),
        StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(), execution_context()
        ),
    )
    result = preparer.prepare(
        JobLease(
            JobId("job_surrogate_ineligible"),
            JobType.STARLINK_SUITE_ANALYSIS,
            starlink_suite_analysis_payload(request),
            1,
            "surrogate-ineligible-token",
            1,
            UtcNs(100),
        )
    )

    assert result.bundle.state is StarlinkSuiteRecordingState.NOT_EVALUATED
    assert result.surrogate_null.bundle.state.value == "not_evaluated"
    assert result.pilot_constellation is None
    assert reader.open_count == 1


class _Catalog:
    def __init__(self) -> None:
        self.by_key: dict[
            str, tuple[StarlinkSurrogateNullCatalogProjectionV0_1, ObjectRef]
        ] = {}
        self.by_analysis: dict[str, CatalogedStarlinkSurrogateNullV0_1] = {}
        self.latest: dict[str, StarlinkSurrogateNullProductRefV0_1] = {}

    def publish_starlink_surrogate_null(
        self,
        projection: StarlinkSurrogateNullCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1:
        if recording_ref.recording_id != projection.recording_id:
            raise ValueError("recording differs")
        candidate = (projection, bundle_ref)
        prior = self.by_key.get(idempotency_key)
        if prior is not None and prior != candidate:
            raise StarlinkSurrogateNullConflictError("conflicting idempotency replay")
        self.by_key[idempotency_key] = candidate
        cataloged = CatalogedStarlinkSurrogateNullV0_1(projection, bundle_ref)
        self.by_analysis[projection.analysis_id] = cataloged
        self.latest[str(projection.recording_id)] = cataloged.ref
        return cataloged.ref

    def get_starlink_surrogate_null(
        self, ref: StarlinkSurrogateNullProductRefV0_1
    ) -> CatalogedStarlinkSurrogateNullV0_1 | None:
        result = self.by_analysis.get(ref.analysis_id)
        return result if result is not None and result.ref == ref else None

    def latest_starlink_surrogate_null(
        self, recording_id: RecordingId
    ) -> StarlinkSurrogateNullProductRefV0_1 | None:
        return self.latest.get(str(recording_id))


def test_preparer_composes_after_existing_suite_without_changing_it(
    prepared: PreparedFixture,
) -> None:
    view, source_request, source_bundle, expected = prepared
    config = _config()
    preparer = StarlinkSurrogateNullAnalysisPreparerV0_1(
        _Reader(view),
        ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(config, execution_context()),
        starlink_search_grid_v0_1(config),
    )
    actual = preparer.prepare_after_suite(source_request, source_bundle)
    same_open_recording = preparer.prepare_from_open_recording(
        cast(RecordingView, view), source_request, source_bundle
    )

    assert actual == expected
    assert same_open_recording == expected
    assert actual.request.source_suite_ref.digest == source_bundle.digest
    assert len(actual.bundle.streams) == 1
    assert len(actual.bundle.streams[0].evidence.surrogates) == 4
    assert source_bundle.digest == actual.request.source_suite_ref.digest


def test_recording_codec_is_canonical_and_closes_nested_evidence(
    prepared: PreparedFixture,
) -> None:
    bundle = prepared[-1].bundle
    payload = encode_starlink_surrogate_null_recording(bundle)

    assert decode_starlink_surrogate_null_recording(payload) == bundle
    assert (
        encode_starlink_surrogate_null_recording(
            decode_starlink_surrogate_null_recording(payload)
        )
        == payload
    )
    with pytest.raises(
        MalformedStarlinkSurrogateNullRecordingError, match="not canonical"
    ):
        decode_starlink_surrogate_null_recording(
            json.dumps(json.loads(payload), indent=2).encode()
        )


def test_durable_store_exact_replay_read_and_conflict_semantics(
    tmp_path: Path, prepared: PreparedFixture
) -> None:
    item = prepared[-1]
    blobs = FileSystemBlobStore(tmp_path / "cas")
    catalog = _Catalog()
    store = DurableStarlinkSurrogateNullStoreV0_1(blobs, catalog)

    first = store.publish(item.request, item.bundle, idempotency_key="surrogate:null")
    second = store.publish(item.request, item.bundle, idempotency_key="surrogate:null")
    assert first == second
    with store.open(first) as durable:
        assert durable.bundle == item.bundle

    projection = starlink_surrogate_null_projection_v0_1(item.request, item.bundle)
    changed_projection = replace(
        projection,
        analysis_id="slsnullrec_ffffffffffffffffffffffffffffffff",
    )
    with pytest.raises(StarlinkSurrogateNullConflictError):
        catalog.publish_starlink_surrogate_null(
            changed_projection,
            first.bundle_ref,
            item.request.recording_object_ref,
            idempotency_key="surrogate:null",
        )


def test_committer_uses_narrow_publisher_boundary(
    tmp_path: Path, prepared: PreparedFixture
) -> None:
    item = prepared[-1]
    store = DurableStarlinkSurrogateNullStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), _Catalog()
    )
    ref = DurableStarlinkSurrogateNullCommitterV0_1(store).commit_surrogate_null(
        item,
        idempotency_key="surrogate:commit",
    )
    assert ref.analysis_id == item.bundle.analysis_id


def test_query_returns_exact_scores_each_surrogate_finite_rank_and_provenance(
    tmp_path: Path, prepared: PreparedFixture
) -> None:
    item = prepared[-1]
    catalog = _Catalog()
    store = DurableStarlinkSurrogateNullStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    store.publish(item.request, item.bundle, idempotency_key="surrogate:query")
    query = StarlinkSurrogateNullQueryV0_1(
        item.bundle.recording_id,
        methods=(StarlinkDetectorMethod.GLRT_32,),
        radio_ids=(RadioId("radio_synthetic"),),
        channel_numbers=(4,),
        edges=(StarlinkEdge.LOWER,),
        interval_start_utc_ns=UtcNs(
            int(item.bundle.streams[0].interval_start_utc_ns) - 1
        ),
        interval_stop_utc_ns=UtcNs(
            int(item.bundle.streams[0].interval_stop_utc_ns) + 1
        ),
    )
    view = DurableRecordingStarlinkSurrogateNullQueryV0_1(
        store, catalog
    ).recording_starlink_surrogate_null(query)

    assert view.total_matching_rows == len(view.rows) == 1
    row = view.rows[0]
    assert row.method is StarlinkDetectorMethod.GLRT_32
    assert len(row.surrogate_scores) == len(row.surrogate_patterns) == 4
    assert (
        row.finite_upper_tail_rank
        == (1 + sum(score >= row.qin_score for score in row.surrogate_scores)) / 5
    )
    assert row.calibrated_p_value is row.calibrated_detection is None
    assert row.provenance.input_digests
    assert view.calibrated_detection_count is None
    assert view.aggregates[0].statistic_semantics.endswith("not-calibrated-p-value")


def test_query_filters_and_row_bound_are_enforced(
    tmp_path: Path, prepared: PreparedFixture
) -> None:
    item = prepared[-1]
    catalog = _Catalog()
    store = DurableStarlinkSurrogateNullStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    store.publish(item.request, item.bundle, idempotency_key="surrogate:bounded")
    query_port = DurableRecordingStarlinkSurrogateNullQueryV0_1(store, catalog)

    excluded = query_port.recording_starlink_surrogate_null(
        StarlinkSurrogateNullQueryV0_1(
            item.bundle.recording_id,
            channel_numbers=(1,),
        )
    )
    assert excluded.total_matching_rows == 0
    bounded = query_port.recording_starlink_surrogate_null(
        StarlinkSurrogateNullQueryV0_1(
            item.bundle.recording_id,
            maximum_rows=1,
        )
    )
    assert bounded.total_matching_rows == 8
    assert len(bounded.rows) == 1
    assert len(bounded.aggregates) == 8


def test_projection_rejects_request_or_source_drift(
    prepared: PreparedFixture,
) -> None:
    item = prepared[-1]
    projection = starlink_surrogate_null_projection_v0_1(item.request, item.bundle)
    assert projection.method_count == 8
    assert projection.surrogate_score_count == 32

    changed = replace(
        item.bundle, request_digest=item.bundle.source_suite_request_digest
    )
    with pytest.raises(
        StarlinkSurrogateNullIntegrityError, match="request and bundle differ"
    ):
        starlink_surrogate_null_projection_v0_1(item.request, changed)


def test_clipped_source_remains_terminal_without_reading_samples() -> None:
    view, _, source_request, source_bundle, config = _fixture()
    view.calls.clear()
    clipped_request = replace(
        source_request,
        stream_selections=(),
        ineligible_reason="clipped-pilot-band",
    )
    clipped_bundle = replace(
        source_bundle,
        analysis_id="slsuite_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        state=StarlinkSuiteRecordingState.NOT_EVALUATED,
        suites=(),
        reason_codes=("clipped-pilot-band",),
    )
    request = starlink_surrogate_null_request_v0_1(
        clipped_request,
        clipped_bundle,
        starlink_search_grid_v0_1(config),
    )
    result = ExactStarlinkSurrogateNullRecordingAnalyzerV0_1(
        config, execution_context()
    ).analyze_surrogate_null(cast(RecordingView, view), request)

    assert result.state.value == "not_evaluated"
    assert result.streams == ()
    assert result.reason_codes == ("clipped-pilot-band",)
    assert view.calls == []
