"""QAM evidence selected by the additive v0.3 multi-basin acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
from .starlink_acquisition import V0_3
from .starlink_pilot_constellation import (
    MAX_CONSTELLATION_POINTS,
    PILOT_SUBCARRIER_COUNT,
    PILOT_SYMBOL_COUNT,
    StarlinkPilotConstellationPointV0_1,
    StarlinkPilotSubcarrierSummaryV0_1,
)


class StarlinkCalibrationState(str, Enum):
    BLOCKED_PENDING_WHOLE_REVISED_SEARCH = "blocked-pending-whole-revised-search"


@dataclass(frozen=True)
class StarlinkRevisedSearchCalibrationIdentityV0_3:
    """Exact maximum whose null distribution must be calibrated as a whole."""

    schema: SchemaRef
    acquisition_algorithm_ref: ArtifactRef
    acquisition_config_ref: ArtifactRef
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    time_window_count: int
    epoch_hypothesis_count: int
    coarse_cfo_hypothesis_count: int
    maximum_coarse_search_cells: int
    maximum_refinement_search_cells: int
    acquire_symbol_indices: tuple[int, ...]
    verify_symbol_indices: tuple[int, ...]
    threshold_state: StarlinkCalibrationState
    calibrated_threshold: float | None

    SCHEMA_ID = "org.leo-flow.starlink-revised-search-calibration-identity"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_3):
            raise ValueError("unsupported revised-search calibration identity")
        for name in (
            "time_window_count",
            "epoch_hypothesis_count",
            "coarse_cfo_hypothesis_count",
            "maximum_coarse_search_cells",
            "maximum_refinement_search_cells",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_coarse_search_cells < (
            self.time_window_count
            * self.epoch_hypothesis_count
            * self.coarse_cfo_hypothesis_count
        ):
            raise ValueError("calibration identity understates the revised maximum")
        if set(self.acquire_symbol_indices) & set(self.verify_symbol_indices):
            raise ValueError("calibration acquire and verify symbols overlap")
        if tuple(
            sorted(self.acquire_symbol_indices + self.verify_symbol_indices)
        ) != tuple(range(2, 302)):
            raise ValueError("calibration identity must bind all pilot symbols")
        if (
            self.threshold_state
            is not StarlinkCalibrationState.BLOCKED_PENDING_WHOLE_REVISED_SEARCH
        ):
            raise ValueError(
                "v0.3 thresholds must remain fail-closed before calibration"
            )
        if self.calibrated_threshold is not None:
            raise ValueError("uncalibrated revised search cannot publish a threshold")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAcquiredPilotConstellationEvidenceV0_3:
    """Diagnostic known-pilot QAM evidence selected by a v0.3 acquisition."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    source_suite_ref: ArtifactRef
    source_acquisition_ref: ArtifactRef
    source_acquisition_search_identity_digest: Digest
    calibration_identity_digest: Digest
    winning_candidate_rank: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    held_out_verify_score: float
    conditioned_control_score: float
    verify_minus_control_margin: float
    constellation_algorithm_ref: ArtifactRef
    constellation_config_ref: ArtifactRef
    residual_cfo_refinement_hz: float
    complete_frame_count: int
    effective_frame_count: float
    hard_symbol_accuracy: float
    rms_evm: float
    model_snr_db: float
    subcarriers: tuple[StarlinkPilotSubcarrierSummaryV0_1, ...]
    points: tuple[StarlinkPilotConstellationPointV0_1, ...]
    provenance: Provenance
    candidate_only: bool
    calibrated_detection: bool | None
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-acquired-pilot-constellation-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_3):
            raise ValueError("unsupported acquired constellation schema")
        require_token(self.analysis_id, "analysis_id")
        if not self.analysis_id.startswith("slqam3_"):
            raise ValueError("invalid acquired constellation identity")
        for name in (
            "sample_rate_hz",
            "winning_cfo_hz",
            "held_out_verify_score",
            "conditioned_control_score",
            "verify_minus_control_margin",
            "residual_cfo_refinement_hz",
            "effective_frame_count",
            "hard_symbol_accuracy",
            "rms_evm",
            "model_snr_db",
        ):
            require_finite(getattr(self, name), name)
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("input dimensions must be positive")
        if self.winning_candidate_rank < 0 or self.winning_epoch_sample < 0:
            raise ValueError("acquisition winner is invalid")
        if (
            abs(
                self.verify_minus_control_margin
                - (self.held_out_verify_score - self.conditioned_control_score)
            )
            > 1e-12
        ):
            raise ValueError("held-out exact/control margin is inconsistent")
        if (
            self.complete_frame_count <= 0
            or not 1 <= self.effective_frame_count <= self.complete_frame_count + 1e-9
        ):
            raise ValueError("frame support is invalid")
        if not 0 <= self.hard_symbol_accuracy <= 1 or self.rms_evm < 0:
            raise ValueError("constellation metrics are invalid")
        if (
            len(self.subcarriers) != PILOT_SUBCARRIER_COUNT
            or len(self.points) != PILOT_SYMBOL_COUNT * PILOT_SUBCARRIER_COUNT
        ):
            raise ValueError(
                "acquired constellation must contain the complete 300 by 8 stack"
            )
        if len(self.points) > MAX_CONSTELLATION_POINTS:
            raise ValueError("acquired constellation is unbounded")
        if not self.candidate_only or self.calibrated_detection is not None:
            raise ValueError("uncalibrated acquired QAM cannot emit a verdict")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "whole-revised-search-calibration-required",
            "conditioned-on-v0.3-multibasin-winner",
            "published-edge-pilot-not-user-payload",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("acquired QAM omits required limitations")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(self.analysis_id, self.digest, self.schema)
