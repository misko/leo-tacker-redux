"""Bounded streaming producer for the independent complete-IQ timeline."""

from __future__ import annotations

import math
from typing import cast

import numpy as np
from numpy.typing import NDArray

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellRefinementRequestV0_1,
    FullDwellRefinementWindowV0_1,
    FullDwellTimelineBundleV0_1,
    FullDwellTimelineRequestV0_1,
    FullDwellTimelineStreamSelectionV0_1,
    FullDwellTimelineStreamV0_1,
    FullDwellTimelineWindowV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext


def contiguous_tile_intervals_v0_1(
    sample_count: int, tile_sample_count: int
) -> tuple[tuple[int, int], ...]:
    """Tile ``[0, sample_count)`` exactly; only the final tile may be short."""
    if sample_count <= 0 or tile_sample_count <= 0:
        raise ValueError("timeline dimensions must be positive")
    return tuple(
        (start, min(start + tile_sample_count, sample_count))
        for start in range(0, sample_count, tile_sample_count)
    )


def _mean_receiver_powers(
    recording: RecordingView,
    segment_id: SegmentId,
    start: int,
    stop: int,
    receiver_count: int,
) -> tuple[float, ...]:
    raw = recording.read_iq_bytes(segment_id, start, stop)
    bytes_per_sample = receiver_count * 4
    if len(raw) % bytes_per_sample:
        raise ValueError("timeline reader returned incomplete CI16 samples")
    count = len(raw) // bytes_per_sample
    if count != stop - start:
        raise ValueError("timeline reader returned another sample interval")
    components = np.frombuffer(raw, dtype="<i2").reshape(count, receiver_count, 2)
    widened = components.astype(np.int64)
    totals = cast(
        NDArray[np.int64],
        np.sum(widened * widened, axis=(0, 2), dtype=np.int64),
    )
    result = tuple(float(value) / count for value in totals)
    if len(result) != receiver_count or any(
        not math.isfinite(value) for value in result
    ):
        raise ValueError("timeline power is not finite")
    return result


class CompleteIqTimelineAnalyzerV0_1:
    """Read at most one bounded tile and one receiver stream at a time."""

    def __init__(self, execution: AnalysisExecutionContext) -> None:
        self._execution = execution

    def analyze(
        self, recording: RecordingView, request: FullDwellTimelineRequestV0_1
    ) -> FullDwellTimelineBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and timeline request identities differ")
        if recording.manifest.radio_id != request.stream_selections[0].radio_id:
            raise ValueError("recording and timeline radio identities differ")
        segments = {item.segment_id: item for item in recording.manifest.segments}
        power_cache: dict[tuple[SegmentId, int, int], tuple[float, ...]] = {}
        streams = tuple(
            self._stream(recording, request, selection, segments, power_cache)
            for selection in request.stream_selections
        )
        token = canonical_digest(
            {
                "request_digest": request.digest,
                "streams": tuple(canonical_digest(item) for item in streams),
            }
        ).value
        recording_digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            canonical_digest(request.plan),
            (recording_digest,),
            (),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        return FullDwellTimelineBundleV0_1(
            SchemaRef(FullDwellTimelineBundleV0_1.SCHEMA_ID, V0_1),
            f"fdtl_{token[:32]}",
            request.recording_id,
            recording_digest,
            request.digest,
            request.plan,
            streams,
            provenance,
            (
                "candidate-evidence-not-calibrated-detection",
                "power-tiles-are-not-starlink-detections",
                "tile-union-covers-every-recorded-sample",
                "base-tiles-are-contiguous-and-nonoverlapping",
                "adjacent-power-tiles-may-be-statistically-dependent",
                "refinement-selected-by-pattern-blind-power-per-stream",
                "selected-refinements-are-a-sparse-dependent-overlay",
            ),
            None,
        )

    def _stream(
        self,
        recording: RecordingView,
        request: FullDwellTimelineRequestV0_1,
        selection: FullDwellTimelineStreamSelectionV0_1,
        segments: dict[SegmentId, SegmentManifest],
        power_cache: dict[tuple[SegmentId, int, int], tuple[float, ...]],
    ) -> FullDwellTimelineStreamV0_1:
        segment = segments.get(selection.segment_id)
        if segment is None:
            raise ValueError("selected timeline segment is unavailable")
        if selection.radio_id != recording.manifest.radio_id:
            raise ValueError("selected timeline radio differs from recording")
        if (
            selection.segment_sample_count != segment.sample_count
            or selection.sample_rate_hz != segment.actual_sample_rate_hz
        ):
            raise ValueError("timeline selection differs from manifest")
        try:
            receiver_index = segment.requested.receiver_chain_ids.index(
                selection.receiver_chain_id
            )
        except ValueError as error:
            raise ValueError("selected timeline receiver is unavailable") from error
        tags = dict(segment.requested.tags)
        if int(tags.get("channel", -1)) != selection.channel_number:
            raise ValueError("timeline channel differs from manifest")
        if tags.get("edge") != selection.edge.value:
            raise ValueError("timeline edge differs from manifest")
        receiver_count = len(segment.requested.receiver_chain_ids)
        intervals = contiguous_tile_intervals_v0_1(
            selection.segment_sample_count, request.plan.tile_sample_count
        )
        if len(intervals) > request.plan.maximum_window_count_per_stream:
            raise ValueError("timeline windows exceed declared bound")
        measured_values = []
        for start, stop in intervals:
            key = (selection.segment_id, start, stop)
            powers = power_cache.get(key)
            if powers is None:
                powers = _mean_receiver_powers(
                    recording,
                    selection.segment_id,
                    start,
                    stop,
                    receiver_count,
                )
                power_cache[key] = powers
            measured_values.append((start, stop, powers[receiver_index]))
        measured = tuple(measured_values)
        selected_count = min(request.plan.maximum_refinements_per_stream, len(measured))
        selected_by_rank = tuple(
            item[0]
            for item in sorted(
                enumerate(measured), key=lambda item: (-item[1][2], item[1][0])
            )[:selected_count]
        )
        rank_by_index = {
            window_index: rank for rank, window_index in enumerate(selected_by_rank)
        }
        windows = tuple(
            FullDwellTimelineWindowV0_1(
                index,
                start,
                stop,
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(start * 1_000_000_000 / selection.sample_rate_hz)
                ),
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(stop * 1_000_000_000 / selection.sample_rate_hz)
                ),
                power,
                rank_by_index.get(index),
            )
            for index, (start, stop, power) in enumerate(measured)
        )
        return FullDwellTimelineStreamV0_1(
            selection.radio_id,
            selection.lnb_id,
            selection.segment_id,
            selection.receiver_chain_id,
            selection.channel_number,
            selection.edge,
            selection.sample_rate_hz,
            selection.segment_sample_count,
            windows,
            selection.segment_sample_count,
            1.0,
            0.0,
            True,
        )


def refinement_request_v0_1(
    request: FullDwellTimelineRequestV0_1,
    bundle: FullDwellTimelineBundleV0_1,
    bundle_digest: Digest,
) -> FullDwellRefinementRequestV0_1:
    """Create the separately dispatchable, bounded exact-work overlay request."""
    if (
        request.recording_id != bundle.recording_id
        or request.digest != bundle.request_digest
    ):
        raise ValueError("timeline request and bundle differ")
    selected = []
    for stream in bundle.streams:
        for window in stream.windows:
            if window.refinement_rank is not None:
                selected.append(
                    FullDwellRefinementWindowV0_1(
                        stream.radio_id,
                        stream.lnb_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.channel_number,
                        stream.edge,
                        window.refinement_rank,
                        window.start_sample,
                        window.stop_sample,
                    )
                )
    selected.sort(
        key=lambda item: (
            str(item.radio_id),
            item.lnb_id,
            str(item.segment_id),
            str(item.receiver_chain_id),
            str(item.channel_number),
            item.edge.value,
            item.rank,
            item.start_sample,
            item.stop_sample,
        )
    )
    return FullDwellRefinementRequestV0_1(
        SchemaRef(FullDwellRefinementRequestV0_1.SCHEMA_ID, V0_1),
        request.recording_id,
        request.recording_object_ref,
        ArtifactRef(bundle.analysis_id, bundle_digest, bundle.schema),
        request.digest,
        tuple(selected),
        request.plan.refinement_selection,
    )
