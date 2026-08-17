"""Compose temporal pilot evidence inside the detector-suite job boundary."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.recording.starlink_temporal_pilot_recording import (
    ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
)
from leo_flow.contracts.core import ArtifactRef, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.contracts.starlink_surrogate_null import StarlinkSearchGridV0_1
from leo_flow.contracts.starlink_temporal_pilot import (
    V0_1,
    StarlinkTemporalPilotRecordingBundleV0_1,
    StarlinkTemporalPilotRequestV0_1,
    StarlinkTemporalProbePlanV0_1,
    StarlinkTemporalStreamSelectionV0_1,
)
from leo_flow.storage.ports import RecordingView

# Measured on the approved 2.5 MS/s Gauss runtime: one 8 ms Qin+four-surrogate
# probe is about 17 s/RX. Five probes over a 20 s dwell keep an eight-worker
# continuous pipeline bounded while sampling the full temporal extent.
DEFAULT_TEMPORAL_STRIDE_SECONDS = 5
DEFAULT_MAXIMUM_TEMPORAL_PROBES = 8


@dataclass(frozen=True)
class PreparedStarlinkTemporalPilotAnalysisV0_1:
    request: StarlinkTemporalPilotRequestV0_1
    bundle: StarlinkTemporalPilotRecordingBundleV0_1


class StarlinkTemporalPilotAnalysisPreparerV0_1:
    def __init__(
        self,
        analyzer: ExactStarlinkTemporalPilotRecordingAnalyzerV0_1,
        search_grid: StarlinkSearchGridV0_1,
        *,
        surrogate_count: int = 4,
        stride_seconds: int = DEFAULT_TEMPORAL_STRIDE_SECONDS,
        maximum_probe_count: int = DEFAULT_MAXIMUM_TEMPORAL_PROBES,
    ) -> None:
        if stride_seconds <= 0:
            raise ValueError("temporal stride_seconds must be positive")
        self._analyzer = analyzer
        self._search_grid = search_grid
        self._surrogate_count = surrogate_count
        self._stride_seconds = stride_seconds
        self._maximum_probe_count = maximum_probe_count

    def prepare_from_open_recording(
        self,
        recording: RecordingView,
        source_request: StarlinkDetectorSuiteRequestV0_2,
        source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    ) -> PreparedStarlinkTemporalPilotAnalysisV0_1:
        request = temporal_pilot_request_v0_1(
            recording,
            source_request,
            source_bundle,
            self._search_grid,
            surrogate_count=self._surrogate_count,
            stride_seconds=self._stride_seconds,
            maximum_probe_count=self._maximum_probe_count,
        )
        return PreparedStarlinkTemporalPilotAnalysisV0_1(
            request, self._analyzer.analyze_temporal_pilot(recording, request)
        )


def temporal_pilot_request_v0_1(
    recording: RecordingView,
    source_request: StarlinkDetectorSuiteRequestV0_2,
    source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    search_grid: StarlinkSearchGridV0_1,
    *,
    surrogate_count: int = 4,
    stride_seconds: int = DEFAULT_TEMPORAL_STRIDE_SECONDS,
    maximum_probe_count: int = DEFAULT_MAXIMUM_TEMPORAL_PROBES,
) -> StarlinkTemporalPilotRequestV0_1:
    if (
        source_request.recording_id != source_bundle.recording_id
        or source_request.recording_id != recording.manifest.recording_id
        or source_request.recording_object_ref.identity_digest()
        != source_bundle.recording_identity_digest
        or source_request.config_ref != search_grid.config_ref
    ):
        raise ValueError("temporal source suite identities differ")
    source_ref = ArtifactRef(
        source_bundle.analysis_id, source_bundle.digest, source_bundle.schema
    )
    source_digest = canonical_digest(source_request)
    if source_bundle.state is StarlinkSuiteRecordingState.NOT_EVALUATED:
        selections: tuple[StarlinkTemporalStreamSelectionV0_1, ...] = ()
        ineligible_reason = "clipped-pilot-band"
        # The plan still records the approved prefix/search resource shape.
        source_selection = (
            source_request.stream_selections[0]
            if source_request.stream_selections
            else None
        )
        window = 1 if source_selection is None else source_selection.probe_sample_count
        stride = window
    else:
        manifest = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        source_by_key = {
            (item.segment_id, item.receiver_chain_id): item
            for item in source_request.stream_selections
        }
        selections_list: list[StarlinkTemporalStreamSelectionV0_1] = []
        for suite in source_bundle.suites:
            key = (suite.segment_id, suite.receiver_chain_id)
            selected = source_by_key.get(key)
            segment = manifest.get(suite.segment_id)
            if selected is None or segment is None:
                raise ValueError("temporal source suite membership differs")
            if (
                selected.edge is not suite.edge
                or selected.probe_sample_count != suite.probe_sample_count
                or segment.sample_count < suite.probe_sample_count
            ):
                raise ValueError("temporal source suite dimensions differ")
            selections_list.append(
                StarlinkTemporalStreamSelectionV0_1(
                    suite.segment_id,
                    suite.receiver_chain_id,
                    suite.edge,
                    suite.sample_rate_hz,
                    segment.sample_count,
                )
            )
        selections = tuple(
            sorted(
                selections_list,
                key=lambda item: (str(item.segment_id), str(item.receiver_chain_id)),
            )
        )
        ineligible_reason = None
        window = source_bundle.suites[0].probe_sample_count
        rates = {item.sample_rate_hz for item in selections}
        if len(rates) != 1:
            raise ValueError("temporal request cannot mix sample rates")
        stride = round(next(iter(rates)) * stride_seconds)
    plan = StarlinkTemporalProbePlanV0_1(
        window, stride, maximum_probe_count, surrogate_count
    )
    return StarlinkTemporalPilotRequestV0_1(
        SchemaRef(StarlinkTemporalPilotRequestV0_1.SCHEMA_ID, V0_1),
        source_request.recording_id,
        source_request.recording_object_ref,
        source_ref,
        source_digest,
        search_grid,
        plan,
        selections,
        SchemaRef(StarlinkTemporalPilotRecordingBundleV0_1.SCHEMA_ID, V0_1),
        ineligible_reason,
    )
