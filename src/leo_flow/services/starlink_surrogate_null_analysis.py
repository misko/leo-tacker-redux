"""Composable preparation boundary for Starlink paired-surrogate evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.contracts.starlink_surrogate_null import V0_1, StarlinkSearchGridV0_1
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullProductRefV0_1,
    StarlinkSurrogateNullRecordingAnalyzerV0_1,
    StarlinkSurrogateNullRecordingBundleV0_1,
    StarlinkSurrogateNullRequestV0_1,
    StarlinkSurrogateNullStreamSelectionV0_1,
)
from leo_flow.storage.ports import RecordingObjectReader, RecordingView


@dataclass(frozen=True)
class PreparedStarlinkSurrogateNullAnalysisV0_1:
    request: StarlinkSurrogateNullRequestV0_1
    bundle: StarlinkSurrogateNullRecordingBundleV0_1


class StarlinkSurrogateNullCommitterV0_1(Protocol):
    def commit_surrogate_null(
        self,
        prepared: PreparedStarlinkSurrogateNullAnalysisV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1: ...


class StarlinkSurrogateNullPublisherV0_1(Protocol):
    def publish(
        self,
        request: StarlinkSurrogateNullRequestV0_1,
        bundle: StarlinkSurrogateNullRecordingBundleV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1: ...


class DurableStarlinkSurrogateNullCommitterV0_1:
    def __init__(self, publisher: StarlinkSurrogateNullPublisherV0_1) -> None:
        self._publisher = publisher

    def commit_surrogate_null(
        self,
        prepared: PreparedStarlinkSurrogateNullAnalysisV0_1,
        *,
        idempotency_key: str,
    ) -> StarlinkSurrogateNullProductRefV0_1:
        return self._publisher.publish(
            prepared.request,
            prepared.bundle,
            idempotency_key=idempotency_key,
        )


class StarlinkSurrogateNullAnalysisPreparerV0_1:
    """Compose immediately after an existing in-memory v0.2 suite result."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        analyzer: StarlinkSurrogateNullRecordingAnalyzerV0_1,
        search_grid: StarlinkSearchGridV0_1,
        *,
        surrogate_count: int = 4,
    ) -> None:
        if (
            isinstance(surrogate_count, bool)
            or not isinstance(surrogate_count, int)
            or not 1 <= surrogate_count <= 32
        ):
            raise ValueError("surrogate_count must lie in [1,32]")
        self._reader = reader
        self._analyzer = analyzer
        self._search_grid = search_grid
        self._surrogate_count = surrogate_count

    def prepare_after_suite(
        self,
        source_request: StarlinkDetectorSuiteRequestV0_2,
        source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    ) -> PreparedStarlinkSurrogateNullAnalysisV0_1:
        request = starlink_surrogate_null_request_v0_1(
            source_request,
            source_bundle,
            self._search_grid,
            surrogate_count=self._surrogate_count,
        )
        with self._reader.open(request.recording_object_ref) as recording:
            return self.prepare_from_open_recording(
                recording,
                source_request,
                source_bundle,
                request=request,
            )

    def prepare_from_open_recording(
        self,
        recording: RecordingView,
        source_request: StarlinkDetectorSuiteRequestV0_2,
        source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
        *,
        request: StarlinkSurrogateNullRequestV0_1 | None = None,
    ) -> PreparedStarlinkSurrogateNullAnalysisV0_1:
        """Compose in one open-recording scope with the existing suite job."""

        expected = starlink_surrogate_null_request_v0_1(
            source_request,
            source_bundle,
            self._search_grid,
            surrogate_count=self._surrogate_count,
        )
        if request is not None and request != expected:
            raise ValueError(
                "provided surrogate-null request differs from suite result"
            )
        bundle = self._analyzer.analyze_surrogate_null(recording, expected)
        return PreparedStarlinkSurrogateNullAnalysisV0_1(expected, bundle)


def starlink_surrogate_null_request_v0_1(
    source_request: StarlinkDetectorSuiteRequestV0_2,
    source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    search_grid: StarlinkSearchGridV0_1,
    *,
    surrogate_count: int = 4,
) -> StarlinkSurrogateNullRequestV0_1:
    """Derive exact stream membership from a completed v0.2 suite product."""

    if (
        source_request.recording_id != source_bundle.recording_id
        or source_request.recording_object_ref.identity_digest()
        != source_bundle.recording_identity_digest
    ):
        raise ValueError("source suite request and result identities differ")
    if source_request.config_ref != search_grid.config_ref:
        raise ValueError("source suite and surrogate search grids differ")
    if source_request.requested_output_schema != SchemaRef(
        StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2
    ):
        raise ValueError("source request does not select a v0.2 suite result")
    source_ref = ArtifactRef(
        source_bundle.analysis_id,
        source_bundle.digest,
        source_bundle.schema,
    )
    source_request_digest = canonical_digest(source_request)
    if source_bundle.state is StarlinkSuiteRecordingState.CANDIDATES:
        expected = {
            (item.segment_id, item.receiver_chain_id): item
            for item in source_request.stream_selections
        }
        actual = {
            (item.segment_id, item.receiver_chain_id): item
            for item in source_bundle.suites
        }
        if expected.keys() != actual.keys():
            raise ValueError("source suite request and result membership differ")
        if any(
            suite.edge is not expected[key].edge
            or suite.probe_sample_count != expected[key].probe_sample_count
            or any(
                method.config_ref != search_grid.config_ref for method in suite.methods
            )
            for key, suite in actual.items()
        ):
            raise ValueError("source suite result differs from its request or grid")
        selections = tuple(
            sorted(
                (
                    StarlinkSurrogateNullStreamSelectionV0_1(
                        item.segment_id,
                        item.receiver_chain_id,
                        item.edge,
                        item.sample_rate_hz,
                        item.probe_sample_count,
                    )
                    for item in source_bundle.suites
                ),
                key=lambda item: (str(item.segment_id), str(item.receiver_chain_id)),
            )
        )
        ineligible_reason = None
    else:
        if source_request.ineligible_reason != "clipped-pilot-band":
            raise ValueError("source not-evaluated reason is not composable")
        selections = ()
        ineligible_reason = "clipped-pilot-band"
    return StarlinkSurrogateNullRequestV0_1(
        SchemaRef(StarlinkSurrogateNullRequestV0_1.SCHEMA_ID, V0_1),
        source_request.recording_id,
        source_request.recording_object_ref,
        source_ref,
        source_request_digest,
        search_grid,
        surrogate_count,
        selections,
        SchemaRef(StarlinkSurrogateNullRecordingBundleV0_1.SCHEMA_ID, V0_1),
        ineligible_reason,
    )
