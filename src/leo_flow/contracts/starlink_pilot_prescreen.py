"""Complete-IQ, pattern-blind OFDM periodicity prescreen contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_full_dwell_timeline_product import (
    FullDwellTimelineStreamSelectionV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

MAXIMUM_PRESCREEN_WINDOWS = 100_000


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenPlanV0_1:
    tile_sample_count: int = 20_000
    maximum_window_count_per_stream: int = MAXIMUM_PRESCREEN_WINDOWS
    maximum_periodicity_seeds_per_stream: int = 32
    maximum_power_seeds_per_stream: int = 8
    selection: str = "union-top-ofdm-periodicity-and-top-power-then-start"

    def __post_init__(self) -> None:
        for name in (
            "tile_sample_count",
            "maximum_window_count_per_stream",
            "maximum_periodicity_seeds_per_stream",
            "maximum_power_seeds_per_stream",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_window_count_per_stream > MAXIMUM_PRESCREEN_WINDOWS:
            raise ValueError("prescreen window bound is too large")
        if (
            self.maximum_periodicity_seeds_per_stream
            + self.maximum_power_seeds_per_stream
            > self.maximum_window_count_per_stream
        ):
            raise ValueError("prescreen seed quotas exceed the window bound")
        if self.selection != "union-top-ofdm-periodicity-and-top-power-then-start":
            raise ValueError("unsupported prescreen selection")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    plan: StarlinkPilotPrescreenPlanV0_1
    streams: tuple[FullDwellTimelineStreamSelectionV0_1, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pilot-prescreen-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported pilot-prescreen request schema")
        if not self.streams:
            raise ValueError("pilot prescreen requires at least one stream")
        identities = tuple(item.identity for item in self.streams)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("pilot-prescreen streams must be canonical and unique")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("pilot-prescreen recording identities differ")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    start_utc_ns: UtcNs
    stop_utc_ns: UtcNs
    mean_power_counts_squared: float
    ofdm_periodicity_score: float
    best_symbol_phase_sample: int
    useful_symbol_lag_samples: int
    total_symbol_samples: int
    periodicity_rank: int | None
    power_rank: int | None

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or int(self.stop_utc_ns) <= int(self.start_utc_ns)
        ):
            raise ValueError("pilot-prescreen window geometry is invalid")
        if not math.isfinite(self.mean_power_counts_squared) or (
            self.mean_power_counts_squared < 0
        ):
            raise ValueError("pilot-prescreen power is invalid")
        if not math.isfinite(self.ofdm_periodicity_score) or not (
            0 <= self.ofdm_periodicity_score <= 1
        ):
            raise ValueError("pilot-prescreen periodicity score is invalid")
        if (
            self.useful_symbol_lag_samples <= 0
            or self.total_symbol_samples <= self.useful_symbol_lag_samples
            or not 0 <= self.best_symbol_phase_sample < self.total_symbol_samples
        ):
            raise ValueError("pilot-prescreen OFDM geometry is invalid")
        for rank in (self.periodicity_rank, self.power_rank):
            if rank is not None and rank < 0:
                raise ValueError("pilot-prescreen seed rank is invalid")

    @property
    def selected_for_exact_refinement(self) -> bool:
        return self.periodicity_rank is not None or self.power_rank is not None


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenStreamV0_1:
    selection: FullDwellTimelineStreamSelectionV0_1
    windows: tuple[StarlinkPilotPrescreenWindowV0_1, ...]
    analyzed_sample_count: int
    coverage_fraction: float

    def __post_init__(self) -> None:
        if not self.windows:
            raise ValueError("pilot-prescreen stream cannot be empty")
        if tuple(item.window_index for item in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("pilot-prescreen window indexes are noncanonical")
        if self.windows[0].start_sample != 0 or any(
            left.stop_sample != right.start_sample
            for left, right in zip(self.windows, self.windows[1:])
        ):
            raise ValueError("pilot-prescreen windows must tile contiguously")
        if self.windows[-1].stop_sample != self.selection.segment_sample_count:
            raise ValueError("pilot-prescreen windows do not cover the segment")
        if self.analyzed_sample_count != self.selection.segment_sample_count:
            raise ValueError("pilot-prescreen analyzed sample count is incomplete")
        if self.coverage_fraction != 1.0:
            raise ValueError("pilot-prescreen coverage must be exact")


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    plan: StarlinkPilotPrescreenPlanV0_1
    streams: tuple[StarlinkPilotPrescreenStreamV0_1, ...]
    provenance: Provenance
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pilot-prescreen-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1) or not self.analysis_id:
            raise ValueError("unsupported pilot-prescreen bundle identity")
        if not self.streams or not self.candidate_only:
            raise ValueError("pilot prescreen is candidate-only evidence")
        required = {
            "complete-iq-ofdm-periodicity-prescreen-not-starlink-detection",
            "pattern-blind-selection-shared-by-qin-and-surrogates",
            "stationary-ofdm-and-tones-may-score-high",
            "exact-target-control-refinement-required",
        }
        if not required.issubset(self.warnings):
            raise ValueError("pilot-prescreen warnings are incomplete")


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        if not self.analysis_id:
            raise ValueError("pilot-prescreen analysis identity cannot be empty")


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenQueryV0_1:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_points: int = 4096

    def __post_init__(self) -> None:
        for values in (
            self.radio_ids,
            self.lnb_ids,
            self.receiver_chain_ids,
            self.edges,
        ):
            if len(values) != len(set(values)):
                raise ValueError("pilot-prescreen filters must be unique")
        if not 1 <= self.maximum_points <= 16_384:
            raise ValueError("pilot-prescreen query point bound is invalid")


@dataclass(frozen=True, slots=True)
class StarlinkPilotPrescreenPresentationStreamV0_1:
    selection: FullDwellTimelineStreamSelectionV0_1
    windows: tuple[StarlinkPilotPrescreenWindowV0_1, ...]
    original_window_count: int
    analyzed_sample_count: int
    coverage_fraction: float


@dataclass(frozen=True, slots=True)
class RecordingStarlinkPilotPrescreenViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    analysis_ref: ArtifactRef
    plan: StarlinkPilotPrescreenPlanV0_1
    streams: tuple[StarlinkPilotPrescreenPresentationStreamV0_1, ...]
    original_window_count: int
    truncated: bool
    decimation: str
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-starlink-pilot-prescreen"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported pilot-prescreen dashboard schema")
        shown = sum(len(stream.windows) for stream in self.streams)
        if self.original_window_count < shown or not self.candidate_only:
            raise ValueError("pilot-prescreen dashboard semantics are invalid")


class RecordingStarlinkPilotPrescreenQueryPortV0_1(Protocol):
    def recording_starlink_pilot_prescreen(
        self, query: StarlinkPilotPrescreenQueryV0_1
    ) -> RecordingStarlinkPilotPrescreenViewV0_1: ...
