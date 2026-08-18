"""Immutable, cheap, complete-IQ timeline and refinement-selection contracts.

This product is intentionally independent of the expensive V15 Qin/surrogate
response.  It may be published as soon as a recording is durable.  A later
worker may use the refinement request, but the timeline never waits for it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .storage import ObjectRef, RecordingObjectRef

V0_1 = SchemaVersion(0, 1)
MAXIMUM_TIMELINE_STREAMS = 16
MAXIMUM_TIMELINE_WINDOWS_PER_STREAM = 16_384
MAXIMUM_TILE_SAMPLE_COUNT = 1_000_000
MAXIMUM_REFINEMENTS_PER_STREAM = 64


@dataclass(frozen=True)
class FullDwellTimelinePlanV0_1:
    tile_sample_count: int
    maximum_window_count_per_stream: int = MAXIMUM_TIMELINE_WINDOWS_PER_STREAM
    maximum_refinements_per_stream: int = 32
    metric: str = "mean-complex-power"
    tiling: str = "contiguous-nonoverlapping-with-short-tail"
    refinement_selection: str = "top-power-then-start;pattern-blind-per-stream"

    def __post_init__(self) -> None:
        if not 1 <= self.tile_sample_count <= MAXIMUM_TILE_SAMPLE_COUNT:
            raise ValueError("timeline tile sample count exceeds its bound")
        if (
            not 1
            <= self.maximum_window_count_per_stream
            <= MAXIMUM_TIMELINE_WINDOWS_PER_STREAM
        ):
            raise ValueError("timeline window count exceeds its bound")
        if (
            not 1
            <= self.maximum_refinements_per_stream
            <= MAXIMUM_REFINEMENTS_PER_STREAM
        ):
            raise ValueError("timeline refinement count exceeds its bound")
        if self.maximum_refinements_per_stream > self.maximum_window_count_per_stream:
            raise ValueError("timeline refinement count exceeds window count")
        if self.metric != "mean-complex-power":
            raise ValueError("unsupported timeline metric")
        if self.tiling != "contiguous-nonoverlapping-with-short-tail":
            raise ValueError("unsupported timeline tiling")
        if self.refinement_selection != "top-power-then-start;pattern-blind-per-stream":
            raise ValueError("unsupported timeline refinement selection")


@dataclass(frozen=True)
class FullDwellTimelineStreamSelectionV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("timeline channel must be one of 1,2,3,4")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.segment_sample_count <= 0:
            raise ValueError("timeline stream dimensions must be positive")

    @property
    def identity(self) -> tuple[str, ...]:
        return tuple(
            map(
                str,
                (
                    self.radio_id,
                    self.lnb_id,
                    self.segment_id,
                    self.receiver_chain_id,
                    self.channel_number,
                    self.edge,
                ),
            )
        )


@dataclass(frozen=True)
class FullDwellTimelineRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    plan: FullDwellTimelinePlanV0_1
    stream_selections: tuple[FullDwellTimelineStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.full-dwell-timeline-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported timeline request schema")
        if self.requested_output_schema != SchemaRef(
            FullDwellTimelineBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported timeline output schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("timeline recording identities differ")
        identities = tuple(item.identity for item in self.stream_selections)
        if not identities or identities != tuple(sorted(identities)):
            raise ValueError("timeline streams must be nonempty and canonical")
        if (
            len(identities) != len(set(identities))
            or len(identities) > MAXIMUM_TIMELINE_STREAMS
        ):
            raise ValueError("timeline streams are duplicate or unbounded")
        for stream in self.stream_selections:
            windows = math.ceil(
                stream.segment_sample_count / self.plan.tile_sample_count
            )
            if windows > self.plan.maximum_window_count_per_stream:
                raise ValueError("timeline geometry exceeds declared window bound")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class FullDwellTimelineWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    mean_complex_power: float
    refinement_rank: int | None

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid timeline window")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("invalid timeline UTC interval")
        require_finite(self.mean_complex_power, "mean_complex_power")
        if self.mean_complex_power < 0:
            raise ValueError("timeline power cannot be negative")
        if self.refinement_rank is not None and self.refinement_rank < 0:
            raise ValueError("timeline refinement rank cannot be negative")

    @property
    def selected_for_exact_refinement(self) -> bool:
        return self.refinement_rank is not None


@dataclass(frozen=True)
class FullDwellTimelineStreamV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    windows: tuple[FullDwellTimelineWindowV0_1, ...]
    covered_sample_count: int
    coverage_fraction: float
    overlap_fraction: float
    refinement_is_data_adaptive: bool

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("timeline channel must be one of 1,2,3,4")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.segment_sample_count <= 0:
            raise ValueError("timeline stream geometry is invalid")
        if tuple(item.window_index for item in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("timeline window indices must be canonical")
        if not self.windows or self.windows[0].start_sample != 0:
            raise ValueError("timeline must start at the first sample")
        if any(
            left.stop_sample != right.start_sample
            for left, right in zip(self.windows, self.windows[1:])
        ):
            raise ValueError("timeline tiles must have no gaps or overlap")
        if self.windows[-1].stop_sample != self.segment_sample_count:
            raise ValueError("timeline must include the final sample")
        if (
            self.covered_sample_count != self.segment_sample_count
            or self.coverage_fraction != 1.0
        ):
            raise ValueError("timeline must account for every sample")
        if self.overlap_fraction != 0.0:
            raise ValueError("timeline base tiles must not overlap")
        ranks = tuple(
            item.refinement_rank
            for item in self.windows
            if item.refinement_rank is not None
        )
        if tuple(sorted(ranks)) != tuple(range(len(ranks))):
            raise ValueError("timeline refinement ranks must be canonical")
        if not self.refinement_is_data_adaptive:
            raise ValueError("timeline refinement dependence must be explicit")

    @property
    def identity(self) -> tuple[str, ...]:
        return tuple(
            map(
                str,
                (
                    self.radio_id,
                    self.lnb_id,
                    self.segment_id,
                    self.receiver_chain_id,
                    self.channel_number,
                    self.edge,
                ),
            )
        )


@dataclass(frozen=True)
class FullDwellTimelineBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    plan: FullDwellTimelinePlanV0_1
    streams: tuple[FullDwellTimelineStreamV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...]
    calibrated_detection_count: None

    SCHEMA_ID = "org.leo-flow.full-dwell-timeline-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported timeline bundle schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("fdtl_"):
            raise ValueError("invalid timeline analysis identity")
        identities = tuple(item.identity for item in self.streams)
        if (
            not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
        ):
            raise ValueError("timeline bundle streams are invalid")
        if self.calibrated_detection_count is not None:
            raise ValueError("timeline cannot contain calibrated detections")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "power-tiles-are-not-starlink-detections",
            "tile-union-covers-every-recorded-sample",
            "base-tiles-are-contiguous-and-nonoverlapping",
            "adjacent-power-tiles-may-be-statistically-dependent",
            "refinement-selected-by-pattern-blind-power-per-stream",
            "selected-refinements-are-a-sparse-dependent-overlay",
        }
        if not required <= set(self.warnings):
            raise ValueError("timeline disclosures are incomplete")
        for stream in self.streams:
            if len(stream.windows) > self.plan.maximum_window_count_per_stream:
                raise ValueError("timeline bundle exceeds its window bound")
            selected_count = sum(
                item.selected_for_exact_refinement for item in stream.windows
            )
            if selected_count > self.plan.maximum_refinements_per_stream:
                raise ValueError("timeline bundle exceeds its refinement bound")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class FullDwellRefinementWindowV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    rank: int
    start_sample: int
    stop_sample: int

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("refinement channel must be one of 1,2,3,4")
        if (
            self.rank < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("invalid refinement window geometry")


@dataclass(frozen=True)
class FullDwellRefinementRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    timeline_ref: ArtifactRef
    timeline_request_digest: Digest
    windows: tuple[FullDwellRefinementWindowV0_1, ...]
    selection_policy: str
    candidate_only: bool = True

    SCHEMA_ID = "org.leo-flow.full-dwell-refinement-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported refinement request schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("refinement recording identities differ")
        if self.selection_policy != "top-power-then-start;pattern-blind-per-stream":
            raise ValueError("unsupported refinement selection policy")
        if not self.candidate_only:
            raise ValueError("refinement request cannot claim detection")
        identities = tuple(
            (
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
            for item in self.windows
        )
        if (
            not identities
            or identities != tuple(sorted(identities))
            or len(identities) != len(set(identities))
            or len(identities)
            > MAXIMUM_TIMELINE_STREAMS * MAXIMUM_REFINEMENTS_PER_STREAM
        ):
            raise ValueError("refinement windows must be canonical and unique")
        grouped: dict[tuple[str, ...], list[int]] = {}
        for item in self.windows:
            key = tuple(
                map(
                    str,
                    (
                        item.radio_id,
                        item.lnb_id,
                        item.segment_id,
                        item.receiver_chain_id,
                        item.channel_number,
                        item.edge,
                    ),
                )
            )
            grouped.setdefault(key, []).append(item.rank)
        if any(tuple(ranks) != tuple(range(len(ranks))) for ranks in grouped.values()):
            raise ValueError("refinement ranks must be canonical per stream")


@dataclass(frozen=True)
class FullDwellTimelineProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef


class FullDwellTimelinePublisherV0_1(Protocol):
    def publish(
        self,
        request: FullDwellTimelineRequestV0_1,
        bundle: FullDwellTimelineBundleV0_1,
        *,
        idempotency_key: str,
    ) -> FullDwellTimelineProductRefV0_1: ...


class FullDwellRefinementDispatchPortV0_1(Protocol):
    """Non-blocking durable admission boundary for the optional exact overlay."""

    def dispatch(self, request: FullDwellRefinementRequestV0_1) -> None: ...
