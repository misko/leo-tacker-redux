"""Exact Qin/surrogate responses selected by the complete-IQ OFDM prescreen."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ._validation import require_finite, require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_adaptive_response import StarlinkAdaptiveResponsePointV0_1
from .starlink_detector_suite import REPORT_METHOD_ORDER
from .starlink_surrogate_null import StarlinkSearchGridV0_1
from .storage import ObjectRef, RecordingObjectRef

MAXIMUM_PILOT_REFINEMENT_STREAMS = 16
MAXIMUM_PILOT_REFINEMENT_SEEDS_PER_STREAM = 40


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementSeedV0_1:
    seed_index: int
    start_sample: int
    stop_sample: int
    ofdm_periodicity_score: float
    mean_power_counts_squared: float
    periodicity_rank: int | None
    power_rank: int | None

    def __post_init__(self) -> None:
        if (
            self.seed_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or (self.periodicity_rank is None and self.power_rank is None)
        ):
            raise ValueError("pilot-refinement seed geometry is invalid")
        require_finite(self.ofdm_periodicity_score, "ofdm_periodicity_score")
        require_finite(self.mean_power_counts_squared, "mean_power_counts_squared")
        if (
            not 0 <= self.ofdm_periodicity_score <= 1
            or self.mean_power_counts_squared < 0
        ):
            raise ValueError("pilot-refinement seed statistic is invalid")
        for rank in (self.periodicity_rank, self.power_rank):
            if rank is not None and rank < 0:
                raise ValueError("pilot-refinement seed rank is invalid")

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(
            reason
            for enabled, reason in (
                (self.periodicity_rank is not None, "top-ofdm-periodicity"),
                (self.power_rank is not None, "top-power"),
            )
            if enabled
        )


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementStreamSelectionV0_1:
    radio_id: RadioId
    lnb_id: str
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    seeds: tuple[StarlinkPilotRefinementSeedV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if (
            self.channel_number not in (1, 2, 3, 4)
            or self.sample_rate_hz <= 0
            or self.segment_sample_count <= 0
            or not 1 <= len(self.seeds) <= MAXIMUM_PILOT_REFINEMENT_SEEDS_PER_STREAM
            or tuple(seed.seed_index for seed in self.seeds)
            != tuple(range(len(self.seeds)))
            or any(seed.stop_sample > self.segment_sample_count for seed in self.seeds)
        ):
            raise ValueError("pilot-refinement stream is invalid")

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


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    source_prescreen_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    source_suite_request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    streams: tuple[StarlinkPilotRefinementStreamSelectionV0_1, ...]
    surrogate_count: int
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-pilot-refinement-request"

    def __post_init__(self) -> None:
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or self.recording_id != self.recording_object_ref.recording_id
            or self.requested_output_schema
            != SchemaRef(StarlinkPilotRefinementBundleV0_1.SCHEMA_ID, V0_1)
            or not 1 <= self.surrogate_count <= 32
        ):
            raise ValueError("pilot-refinement request identity is invalid")
        identities = tuple(stream.identity for stream in self.streams)
        if (
            not identities
            or identities != tuple(sorted(set(identities)))
            or len(identities) > MAXIMUM_PILOT_REFINEMENT_STREAMS
        ):
            raise ValueError("pilot-refinement streams are noncanonical")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementStreamV0_1:
    selection: StarlinkPilotRefinementStreamSelectionV0_1
    points: tuple[StarlinkAdaptiveResponsePointV0_1, ...]
    exact_covered_sample_count: int
    exact_coverage_fraction: float

    def __post_init__(self) -> None:
        expected = len(self.selection.seeds) * len(REPORT_METHOD_ORDER)
        if len(self.points) != expected:
            raise ValueError("pilot-refinement method coverage is incomplete")
        if not math.isclose(
            self.exact_coverage_fraction,
            self.exact_covered_sample_count / self.selection.segment_sample_count,
            abs_tol=1e-15,
        ):
            raise ValueError("pilot-refinement coverage is inconsistent")


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementBundleV0_1:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_prescreen_ref: ArtifactRef
    source_suite_ref: ArtifactRef
    request_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    streams: tuple[StarlinkPilotRefinementStreamV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...]
    calibrated_detection_count: None

    SCHEMA_ID = "org.leo-flow.starlink-pilot-refinement-bundle"

    def __post_init__(self) -> None:
        required = {
            "candidate-evidence-not-calibrated-detection",
            "complete-iq-selection-shared-by-qin-and-surrogates",
            "time-epoch-cfo-look-elsewhere-calibration-required",
            "stationary-ofdm-and-tones-may-enter-refinement",
        }
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or not self.analysis_id
            or not self.streams
            or not required.issubset(self.warnings)
        ):
            raise ValueError("pilot-refinement bundle semantics are invalid")


@dataclass(frozen=True, slots=True)
class StarlinkPilotRefinementProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef
