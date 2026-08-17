"""Bounded recording bridge for paired Starlink surrogate controls."""

from __future__ import annotations

from leo_flow.contracts.core import Digest, SchemaRef, UtcNs, canonical_digest
from leo_flow.contracts.starlink_surrogate_null import V0_1
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullRecordingBundleV0_1,
    StarlinkSurrogateNullRecordingState,
    StarlinkSurrogateNullRequestV0_1,
    StarlinkSurrogateNullStreamEvidenceV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext
from .quality import decode_ci16
from .starlink_detector_suite import StarlinkDetectorSuiteConfigV0_2
from .starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    radio_signal_v0_1,
    starlink_search_grid_v0_1,
)


class ExactStarlinkSurrogateNullRecordingAnalyzerV0_1:
    """Analyze exactly the source-suite stream membership and sample prefixes."""

    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._analyzer = StarlinkPairedSurrogateAnalyzerV0_1(
            ReportMethodStarlinkDetectorV0_1(execution),
            config,
        )

    def analyze_surrogate_null(
        self,
        recording: RecordingView,
        request: StarlinkSurrogateNullRequestV0_1,
    ) -> StarlinkSurrogateNullRecordingBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and surrogate-null request identities differ")
        if request.search_grid != starlink_search_grid_v0_1(self._config):
            raise ValueError("surrogate-null request selects another search grid")
        recording_digest = request.recording_object_ref.identity_digest()
        if request.ineligible_reason is not None:
            return _recording_bundle(request, recording_digest, ())

        segments = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        streams: list[StarlinkSurrogateNullStreamEvidenceV0_1] = []
        for selection in request.stream_selections:
            try:
                segment = segments[selection.segment_id]
                receiver_index = segment.requested.receiver_chain_ids.index(
                    selection.receiver_chain_id
                )
            except (KeyError, ValueError) as error:
                raise ValueError(
                    "selected surrogate-null stream is unavailable"
                ) from error
            if selection.probe_sample_count > segment.sample_count:
                raise ValueError("surrogate-null probe exceeds selected segment")
            if segment.actual_sample_rate_hz != selection.sample_rate_hz:
                raise ValueError("surrogate-null sample rate differs from selection")
            tags = dict(segment.requested.tags)
            try:
                channel_number = int(tags["channel"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("surrogate-null stream lacks a channel tag") from error
            if tags.get("edge") != selection.edge.value:
                raise ValueError("surrogate-null edge differs from segment tag")
            raw = recording.read_iq_bytes(
                selection.segment_id,
                0,
                selection.probe_sample_count,
            )
            values, count = decode_ci16(raw, len(segment.requested.receiver_chain_ids))
            if count != selection.probe_sample_count:
                raise ValueError("surrogate-null reader returned another interval")
            stride = len(segment.requested.receiver_chain_ids) * 2
            offset = receiver_index * 2
            samples = tuple(
                complex(values[position], values[position + 1])
                for position in range(offset, count * stride, stride)
            )
            signal = radio_signal_v0_1(
                samples,
                recording_id=request.recording_id,
                recording_identity_digest=recording_digest,
                segment_id=selection.segment_id,
                receiver_chain_id=selection.receiver_chain_id,
                edge=selection.edge,
                sample_rate_hz=selection.sample_rate_hz,
            )
            evidence = self._analyzer.analyze(
                signal,
                surrogate_count=request.surrogate_count,
            )
            stop_ns = UtcNs(
                int(segment.start_utc_ns)
                + round(
                    selection.probe_sample_count
                    / selection.sample_rate_hz
                    * 1_000_000_000
                )
            )
            streams.append(
                StarlinkSurrogateNullStreamEvidenceV0_1(
                    recording.manifest.radio_id,
                    selection.segment_id,
                    selection.receiver_chain_id,
                    channel_number,
                    selection.edge,
                    segment.start_utc_ns,
                    stop_ns,
                    evidence,
                )
            )
        streams.sort(
            key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
        )
        return _recording_bundle(request, recording_digest, tuple(streams))


def _recording_bundle(
    request: StarlinkSurrogateNullRequestV0_1,
    recording_digest: Digest,
    streams: tuple[StarlinkSurrogateNullStreamEvidenceV0_1, ...],
) -> StarlinkSurrogateNullRecordingBundleV0_1:
    state = (
        StarlinkSurrogateNullRecordingState.CANDIDATES
        if streams
        else StarlinkSurrogateNullRecordingState.NOT_EVALUATED
    )
    reason_codes = (
        (
            "finite-paired-surrogate-controls",
            "not-calibrated-p-values",
            "not-calibrated-detections",
        )
        if streams
        else ("clipped-pilot-band",)
    )
    token = canonical_digest(
        {
            "request_digest": request.digest,
            "state": state.value,
            "stream_digests": tuple(item.evidence.digest for item in streams),
        }
    ).value
    return StarlinkSurrogateNullRecordingBundleV0_1(
        SchemaRef(StarlinkSurrogateNullRecordingBundleV0_1.SCHEMA_ID, V0_1),
        f"slsnullrec_{token[:32]}",
        request.recording_id,
        recording_digest,
        request.source_suite_ref,
        request.source_suite_request_digest,
        request.digest,
        state,
        streams,
        reason_codes,
        None,
    )
