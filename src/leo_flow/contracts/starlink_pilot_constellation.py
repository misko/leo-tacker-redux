"""Bounded constellation evidence for the published Starlink edge pilots.

This contract represents a diagnostic of a selected detector-suite candidate.  It
does not represent payload demodulation and it cannot carry a detection verdict.
"""

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
    SegmentId,
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_detector_suite import StarlinkDetectorMethod

MAX_CONSTELLATION_POINTS = 2_400
PILOT_SUBCARRIER_COUNT = 8
PILOT_SYMBOL_COUNT = 300


@dataclass(frozen=True)
class StarlinkPilotConstellationPointV0_1:
    """One cross-fitted, equalized known-pilot coefficient."""

    symbol_index: int
    subcarrier_index: int
    expected_state: int
    hard_state: int
    i: float
    q: float
    correct: bool
    confidence: float
    expected_probability: float
    entropy_bits: float

    def __post_init__(self) -> None:
        if not 2 <= self.symbol_index <= 301:
            raise ValueError("pilot symbol index must lie in 2..301")
        if self.subcarrier_index not in (*range(488, 496), *range(528, 536)):
            raise ValueError("subcarrier is not in a published edge-pilot band")
        if self.expected_state not in range(4) or self.hard_state not in range(4):
            raise ValueError("4QAM/QPSK states must lie in 0..3")
        if self.correct != (self.expected_state == self.hard_state):
            raise ValueError("point correctness conflicts with its states")
        for name in ("i", "q", "confidence", "expected_probability", "entropy_bits"):
            require_finite(getattr(self, name), name)
        if not 0.25 <= self.confidence <= 1:
            raise ValueError("four-state confidence must lie in [0.25, 1]")
        if not 0 <= self.expected_probability <= 1:
            raise ValueError("expected probability must lie in [0, 1]")
        if not 0 <= self.entropy_bits <= 2:
            raise ValueError("four-state entropy must lie in [0, 2]")


@dataclass(frozen=True)
class StarlinkPilotSubcarrierSummaryV0_1:
    subcarrier_index: int
    offset_from_edge_center_hz: float
    hard_symbol_accuracy: float
    rms_evm: float
    channel_magnitude: float
    channel_phase_deg: float

    def __post_init__(self) -> None:
        if self.subcarrier_index not in (*range(488, 496), *range(528, 536)):
            raise ValueError("subcarrier is not in a published edge-pilot band")
        for name in (
            "offset_from_edge_center_hz",
            "hard_symbol_accuracy",
            "rms_evm",
            "channel_magnitude",
            "channel_phase_deg",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.hard_symbol_accuracy <= 1:
            raise ValueError("hard-symbol accuracy must lie in [0, 1]")
        if self.rms_evm < 0 or self.channel_magnitude < 0:
            raise ValueError("EVM and channel magnitude cannot be negative")


@dataclass(frozen=True)
class StarlinkPilotConstellationEvidenceV0_1:
    """Diagnostic constellation conditioned on one full-frame acquire winner."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    source_suite_analysis_id: str
    source_suite_digest: Digest
    source_suite_identity_digest: Digest
    selection_method: StarlinkDetectorMethod
    acquire_search_identity_digest: Digest
    acquire_algorithm_ref: ArtifactRef
    acquire_config_ref: ArtifactRef
    exact_template_ref: ArtifactRef
    winning_epoch_sample: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    residual_cfo_refinement_hz: float
    complete_frame_count: int
    effective_frame_count: float
    stacking_gain_db: float
    observation_count: int
    hard_symbol_accuracy: float
    random_chance_accuracy: float
    rms_evm: float
    median_equalized_magnitude: float
    soft_mean_confidence: float
    soft_mean_expected_probability: float
    soft_mean_entropy_bits: float
    soft_noise_variance: float
    model_snr_db: float
    subcarriers: tuple[StarlinkPilotSubcarrierSummaryV0_1, ...]
    points: tuple[StarlinkPilotConstellationPointV0_1, ...]
    point_selection: str
    provenance: Provenance
    candidate_only: bool
    known_synchronization_pilot: bool
    payload_decoded: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pilot-constellation-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported Starlink pilot constellation schema")
        require_token(self.analysis_id, "analysis_id")
        require_token(self.source_suite_analysis_id, "source_suite_analysis_id")
        if self.selection_method is not StarlinkDetectorMethod.FULL_FRAME_ACQUIRE:
            raise ValueError("constellation must bind the full-frame acquire winner")
        for name in (
            "sample_rate_hz",
            "winning_coarse_cfo_hz",
            "winning_residual_cfo_hz",
            "residual_cfo_refinement_hz",
            "effective_frame_count",
            "stacking_gain_db",
            "hard_symbol_accuracy",
            "random_chance_accuracy",
            "rms_evm",
            "median_equalized_magnitude",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
            "soft_mean_entropy_bits",
            "soft_noise_variance",
            "model_snr_db",
        ):
            require_finite(getattr(self, name), name)
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("input dimensions must be positive")
        if self.winning_epoch_sample < 0 or self.complete_frame_count <= 0:
            raise ValueError("winner epoch and complete-frame support are invalid")
        if not 1 <= self.effective_frame_count <= self.complete_frame_count + 1e-9:
            raise ValueError("effective frame count lies outside its support")
        if self.observation_count != PILOT_SYMBOL_COUNT * PILOT_SUBCARRIER_COUNT:
            raise ValueError("stacked constellation must contain 300 by 8 observations")
        for name in (
            "hard_symbol_accuracy",
            "random_chance_accuracy",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0, 1]")
        if self.random_chance_accuracy != 0.25:
            raise ValueError("published edge pilots use four equiprobable states")
        if not 0 <= self.soft_mean_entropy_bits <= 2:
            raise ValueError("mean four-state entropy must lie in [0, 2]")
        if self.rms_evm < 0 or self.soft_noise_variance <= 0:
            raise ValueError("EVM must be non-negative and noise variance positive")
        expected_indices = (
            tuple(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else tuple(range(488, 496))
        )
        if (
            tuple(item.subcarrier_index for item in self.subcarriers)
            != expected_indices
        ):
            raise ValueError(
                "subcarrier summaries must cover the selected edge in order"
            )
        if not self.points or len(self.points) > MAX_CONSTELLATION_POINTS:
            raise ValueError("constellation point count lies outside its bound")
        if any(item.subcarrier_index not in expected_indices for item in self.points):
            raise ValueError("constellation point belongs to another edge")
        identities = [
            (item.symbol_index, item.subcarrier_index) for item in self.points
        ]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError(
                "constellation points must be unique and canonically ordered"
            )
        if self.point_selection != "quality-weighted-stack-all-300x8-cross-fitted":
            raise ValueError("unknown constellation point selection")
        if not self.candidate_only or not self.known_synchronization_pilot:
            raise ValueError("constellation is candidate evidence for a known pilot")
        if self.payload_decoded:
            raise ValueError("edge-pilot constellation cannot claim payload decoding")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "published-edge-pilot-not-user-payload",
            "conditioned-on-full-frame-acquire-winner",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("constellation evidence must disclose selection and scope")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
