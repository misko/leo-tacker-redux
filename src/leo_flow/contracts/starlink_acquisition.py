"""Additive, bounded Starlink edge-pilot acquisition evidence."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_finite, require_token
from .core import (
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    canonical_digest,
)
from .starlink import StarlinkEdge

V0_3 = SchemaVersion(0, 3)


@dataclass(frozen=True)
class StarlinkAcquisitionCandidateV0_3:
    """One retained basin, refined before held-out adjudication."""

    coarse_epoch_sample: int
    coarse_cfo_hz: float
    coarse_score: float
    refined_epoch_sample: int
    refined_cfo_hz: float
    acquire_score: float
    verify_score: float
    conditioned_control_score: float
    verify_minus_control_margin: float
    frame_support: int
    rank: int

    def __post_init__(self) -> None:
        for name in ("coarse_epoch_sample", "refined_epoch_sample", "rank"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.frame_support <= 0:
            raise ValueError("candidate frame support must be positive")
        for name in (
            "coarse_cfo_hz",
            "coarse_score",
            "refined_cfo_hz",
            "acquire_score",
            "verify_score",
            "conditioned_control_score",
            "verify_minus_control_margin",
        ):
            require_finite(getattr(self, name), name)
        for name in (
            "coarse_score",
            "acquire_score",
            "verify_score",
            "conditioned_control_score",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0, 1]")
        if (
            abs(
                self.verify_minus_control_margin
                - (self.verify_score - self.conditioned_control_score)
            )
            > 1e-12
        ):
            raise ValueError("candidate verify/control margin is inconsistent")


@dataclass(frozen=True)
class StarlinkAcquisitionBundleV0_3:
    """Receiver-specific v0.3 acquisition output; never a detection verdict."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    receiver_cfo_profile_id: str
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    search_identity_digest: Digest
    searched_cfo_min_hz: float
    searched_cfo_max_hz: float
    coarse_search_cell_count: int
    refinement_search_cell_count: int
    peak_count_before_retention: int
    candidates: tuple[StarlinkAcquisitionCandidateV0_3, ...]
    winning_candidate_rank: int
    acquire_symbol_indices: tuple[int, ...]
    verify_symbol_indices: tuple[int, ...]
    provenance: Provenance
    candidates_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-edge-pilot-acquisition"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_3):
            raise ValueError("unsupported Starlink acquisition schema")
        require_token(self.analysis_id, "analysis_id")
        require_token(self.receiver_cfo_profile_id, "receiver_cfo_profile_id")
        for name in (
            "sample_rate_hz",
            "searched_cfo_min_hz",
            "searched_cfo_max_hz",
        ):
            require_finite(getattr(self, name), name)
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("acquisition input dimensions must be positive")
        if self.searched_cfo_min_hz >= self.searched_cfo_max_hz:
            raise ValueError("searched CFO domain must be non-empty")
        if self.coarse_search_cell_count <= 0 or self.refinement_search_cell_count <= 0:
            raise ValueError("search cell counts must be positive")
        if self.peak_count_before_retention < len(self.candidates):
            raise ValueError("retained candidates exceed discovered peaks")
        if not self.candidates:
            raise ValueError("acquisition requires at least one refined candidate")
        if tuple(item.rank for item in self.candidates) != tuple(
            range(len(self.candidates))
        ):
            raise ValueError("candidate ranks must be canonical and contiguous")
        if not 0 <= self.winning_candidate_rank < len(self.candidates):
            raise ValueError("winning candidate rank is outside retained candidates")
        if set(self.acquire_symbol_indices) & set(self.verify_symbol_indices):
            raise ValueError("acquire and verify pilot symbols must be disjoint")
        if tuple(
            sorted(self.acquire_symbol_indices + self.verify_symbol_indices)
        ) != tuple(range(2, 302)):
            raise ValueError("acquire and verify must partition pilot symbols 2..301")
        if not self.candidates_only:
            raise ValueError("uncalibrated acquisition cannot emit a verdict")
        required = {
            "held-out-pilot-adjudication",
            "whole-revised-search-calibration-required",
            "known-published-pilot-not-user-payload",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("acquisition evidence omits required limitations")

    @property
    def winner(self) -> StarlinkAcquisitionCandidateV0_3:
        return self.candidates[self.winning_candidate_rank]

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_3),
        )
