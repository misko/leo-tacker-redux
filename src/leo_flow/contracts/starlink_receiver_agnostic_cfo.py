"""Immutable v0.6 contract for pattern-symmetric residual-CFO search."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite
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
    canonical_digest,
)
from .starlink import StarlinkEdge
from .starlink_adaptive_calibration import AdaptivePatternRole

V0_6 = SchemaVersion(0, 6)
MINIMUM_RESIDUAL_CFO_HZ = 700_000.0
MAXIMUM_CFO_PATTERNS = 33
HARD_MAXIMUM_COARSE_CELLS = 100_000
HARD_MAXIMUM_LOCAL_CELLS = 100_000
HARD_MAXIMUM_UNIQUE_CELLS = 150_000
HARD_MAXIMUM_PATTERN_EVALUATIONS = 1_000_000


class ReceiverAgnosticCfoCellStage(str, Enum):
    COARSE = "declared-domain-coarse"
    LOCAL = "pattern-symmetric-multibasin-local"


@dataclass(frozen=True)
class ReceiverAgnosticCfoSearchPlanV0_6:
    """One physical residual domain used unchanged for every receiver."""

    cfo_min_hz: float = -700_000.0
    cfo_max_hz: float = 700_000.0
    coarse_cfo_step_hz: float = 100_000.0
    local_cfo_radius_hz: float = 100_000.0
    local_cfo_step_hz: float = 5_000.0
    coarse_epoch_stride_samples: int = 8
    local_epoch_radius_samples: int = 7
    basins_per_pattern: int = 3
    basin_cfo_separation_hz: float = 100_000.0
    basin_epoch_separation_samples: int = 8
    maximum_coarse_cells: int = 100_000
    maximum_local_cells: int = 100_000
    maximum_unique_cells: int = 150_000
    maximum_pattern_evaluations: int = 1_000_000
    receiver_adjustment_policy: str = "none-residual-domain-is-identical-for-every-radio-rx"
    pattern_search_policy: str = "equal-basin-quota-then-all-patterns-search-union"

    def __post_init__(self) -> None:
        for name in (
            "cfo_min_hz",
            "cfo_max_hz",
            "coarse_cfo_step_hz",
            "local_cfo_radius_hz",
            "local_cfo_step_hz",
            "basin_cfo_separation_hz",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.cfo_min_hz > -MINIMUM_RESIDUAL_CFO_HZ
            or self.cfo_max_hz < MINIMUM_RESIDUAL_CFO_HZ
        ):
            raise ValueError("v0.6 residual CFO domain must cover at least -700 to +700 kHz")
        if self.cfo_min_hz >= self.cfo_max_hz:
            raise ValueError("residual CFO domain must be non-empty")
        if not math.isfinite(self.cfo_max_hz - self.cfo_min_hz):
            raise ValueError("residual CFO span must be finite")
        if min(
            self.coarse_cfo_step_hz,
            self.local_cfo_radius_hz,
            self.local_cfo_step_hz,
            self.basin_cfo_separation_hz,
        ) <= 0:
            raise ValueError("CFO search steps, radius, and separation must be positive")
        for name in (
            "coarse_epoch_stride_samples",
            "local_epoch_radius_samples",
            "basins_per_pattern",
            "basin_epoch_separation_samples",
            "maximum_coarse_cells",
            "maximum_local_cells",
            "maximum_unique_cells",
            "maximum_pattern_evaluations",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.local_cfo_radius_hz < self.coarse_cfo_step_hz:
            raise ValueError("local CFO radius must bridge adjacent coarse cells")
        if self.local_epoch_radius_samples < self.coarse_epoch_stride_samples - 1:
            raise ValueError("local epoch radius must cover every epoch residue")
        if self.receiver_adjustment_policy != (
            "none-residual-domain-is-identical-for-every-radio-rx"
        ):
            raise ValueError("receiver-specific CFO adjustments are forbidden")
        if self.pattern_search_policy != (
            "equal-basin-quota-then-all-patterns-search-union"
        ):
            raise ValueError("unsupported pattern search policy")
        if (
            self.maximum_coarse_cells > HARD_MAXIMUM_COARSE_CELLS
            or self.maximum_local_cells > HARD_MAXIMUM_LOCAL_CELLS
            or self.maximum_unique_cells > HARD_MAXIMUM_UNIQUE_CELLS
            or self.maximum_pattern_evaluations
            > HARD_MAXIMUM_PATTERN_EVALUATIONS
        ):
            raise ValueError("CFO search resource ceiling exceeds v0.6 hard bound")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticCfoPatternV0_6:
    pattern_index: int
    role: AdaptivePatternRole
    template_digest: Digest

    def __post_init__(self) -> None:
        expected = (
            AdaptivePatternRole.QIN
            if self.pattern_index == 0
            else AdaptivePatternRole.SURROGATE
        )
        if self.pattern_index < 0 or self.role is not expected:
            raise ValueError("CFO pattern role/index is invalid")


@dataclass(frozen=True)
class ReceiverAgnosticCfoCellV0_6:
    cell_index: int
    stage: ReceiverAgnosticCfoCellStage
    epoch_sample: int
    cfo_hz: float
    selected_by_pattern_indices: tuple[int, ...]
    pattern_scores: tuple[float, ...]

    def __post_init__(self) -> None:
        require_finite(self.cfo_hz, "cfo_hz")
        if self.cell_index < 0 or self.epoch_sample < 0:
            raise ValueError("CFO cell coordinates are invalid")
        if self.selected_by_pattern_indices != tuple(
            sorted(set(self.selected_by_pattern_indices))
        ):
            raise ValueError("CFO cell selectors are noncanonical")
        if not self.pattern_scores:
            raise ValueError("CFO cell omits pattern scores")
        for score in self.pattern_scores:
            require_finite(score, "pattern_score")
            if not 0 <= score <= 1:
                raise ValueError("CFO pattern score must lie in [0,1]")


@dataclass(frozen=True)
class ReceiverAgnosticCfoWinnerV0_6:
    pattern_index: int
    cell_index: int
    epoch_sample: int
    cfo_hz: float
    score: float

    def __post_init__(self) -> None:
        require_finite(self.cfo_hz, "cfo_hz")
        require_finite(self.score, "score")
        if min(self.pattern_index, self.cell_index, self.epoch_sample) < 0:
            raise ValueError("CFO winner coordinates are invalid")
        if not 0 <= self.score <= 1:
            raise ValueError("CFO winner score must lie in [0,1]")


@dataclass(frozen=True)
class ReceiverAgnosticCfoSearchReceiptV0_6:
    """Exact searched family and scores; deliberately not a detection result."""

    schema: SchemaRef
    plan: ReceiverAgnosticCfoSearchPlanV0_6
    epoch_modulus_samples: int
    patterns: tuple[ReceiverAgnosticCfoPatternV0_6, ...]
    cells: tuple[ReceiverAgnosticCfoCellV0_6, ...]
    winners: tuple[ReceiverAgnosticCfoWinnerV0_6, ...]
    coarse_cell_count: int
    local_cell_count: int
    unique_cell_count: int
    pattern_evaluation_count: int
    look_elsewhere_hypothesis_count: int
    candidates_only: bool
    calibrated_detection_count: None
    disclosures: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.receiver-agnostic-cfo-search-receipt"

    def __post_init__(self) -> None:
        required = {
            "candidate-evidence-not-calibrated-detection",
            "no-detection-threshold-or-calibration-claim",
            "identical-residual-cfo-domain-for-every-radio-rx",
            "qin-and-surrogates-search-identical-cell-union",
            "look-elsewhere-family-is-exact-pattern-by-unique-cell-product",
            "retro-and-j1-are-conditioned-numerical-canaries-only",
        }
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_6):
            raise ValueError("unsupported receiver-agnostic CFO receipt schema")
        if self.epoch_modulus_samples <= 0:
            raise ValueError("epoch modulus must be positive")
        if (
            not self.patterns
            or len(self.patterns) > MAXIMUM_CFO_PATTERNS
            or tuple(item.pattern_index for item in self.patterns)
            != tuple(range(len(self.patterns)))
            or len({item.template_digest for item in self.patterns})
            != len(self.patterns)
        ):
            raise ValueError("CFO search patterns are invalid")
        if tuple(item.cell_index for item in self.cells) != tuple(range(len(self.cells))):
            raise ValueError("CFO cell indexes are noncanonical")
        coordinates = tuple((item.epoch_sample, item.cfo_hz) for item in self.cells)
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("CFO cells are duplicated")
        if any(
            item.epoch_sample >= self.epoch_modulus_samples
            or len(item.pattern_scores) != len(self.patterns)
            or any(index >= len(self.patterns) for index in item.selected_by_pattern_indices)
            for item in self.cells
        ):
            raise ValueError("CFO cell evidence differs from declared search")
        if tuple(item.pattern_index for item in self.winners) != tuple(
            range(len(self.patterns))
        ):
            raise ValueError("CFO winners are noncanonical")
        if any(
            winner.cell_index >= len(self.cells)
            or (
                winner.epoch_sample,
                winner.cfo_hz,
                winner.score,
            )
            != (
                self.cells[winner.cell_index].epoch_sample,
                self.cells[winner.cell_index].cfo_hz,
                self.cells[winner.cell_index].pattern_scores[winner.pattern_index],
            )
            for winner in self.winners
        ):
            raise ValueError("CFO winners do not refer to exact evaluated cells")
        expected_evaluations = len(self.cells) * len(self.patterns)
        if (
            self.coarse_cell_count <= 0
            or self.local_cell_count <= 0
            or self.coarse_cell_count > self.plan.maximum_coarse_cells
            or self.local_cell_count > self.plan.maximum_local_cells
            or self.unique_cell_count != len(self.cells)
            or self.unique_cell_count > self.plan.maximum_unique_cells
            or self.pattern_evaluation_count != expected_evaluations
            or self.pattern_evaluation_count
            > self.plan.maximum_pattern_evaluations
            or self.look_elsewhere_hypothesis_count != expected_evaluations
            or not self.candidates_only
            or self.calibrated_detection_count is not None
            or not required <= set(self.disclosures)
        ):
            raise ValueError("CFO search cost or limitations are inconsistent")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticCfoWindowV0_6:
    """Exact raw-IQ interval and physical receiver provenance; never a CFO hint."""

    recording_id: RecordingId
    recording_identity_digest: Digest
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    start_sample: int
    stop_sample: int
    source_recording_ref: ArtifactRef
    source_window_ref: ArtifactRef

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if (
            self.sample_rate_hz <= 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("receiver-agnostic CFO window is invalid")

    @property
    def sample_count(self) -> int:
        return self.stop_sample - self.start_sample

    @property
    def identity(self) -> tuple[str, str, str, int, int]:
        return (
            str(self.recording_id),
            str(self.segment_id),
            str(self.receiver_chain_id),
            self.start_sample,
            self.stop_sample,
        )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReceiverAgnosticPatternQamEvidenceV0_6:
    """Known-pattern QAM evaluated at that pattern's own v0.6 winner."""

    pattern_index: int
    role: AdaptivePatternRole
    template_ref: ArtifactRef
    control_template_ref: ArtifactRef
    winner: ReceiverAgnosticCfoWinnerV0_6
    complete_frame_count: int
    hard_symbol_accuracy: float
    rms_evm: float
    qam_goodness: float

    def __post_init__(self) -> None:
        expected = (
            AdaptivePatternRole.QIN
            if self.pattern_index == 0
            else AdaptivePatternRole.SURROGATE
        )
        for name in ("hard_symbol_accuracy", "rms_evm", "qam_goodness"):
            require_finite(getattr(self, name), name)
        if (
            self.pattern_index < 0
            or self.role is not expected
            or self.winner.pattern_index != self.pattern_index
            or self.template_ref.digest == self.control_template_ref.digest
            or self.complete_frame_count <= 0
            or not 0 <= self.hard_symbol_accuracy <= 1
            or self.rms_evm < 0
            or not 0 <= self.qam_goodness <= 1
        ):
            raise ValueError("receiver-agnostic pattern QAM evidence is invalid")


@dataclass(frozen=True)
class ReceiverAgnosticCfoQamWindowBundleV0_6:
    """Additive raw-window CFO/QAM product; not storage or a detection verdict."""

    schema: SchemaRef
    analysis_id: str
    window: ReceiverAgnosticCfoWindowV0_6
    search_algorithm_ref: ArtifactRef
    scorer_algorithm_ref: ArtifactRef
    qam_algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    search_receipt: ReceiverAgnosticCfoSearchReceiptV0_6
    pattern_qam: tuple[ReceiverAgnosticPatternQamEvidenceV0_6, ...]
    provenance: Provenance
    candidates_only: bool
    calibrated_detection_count: None
    disclosures: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.receiver-agnostic-cfo-qam-window-bundle"

    def __post_init__(self) -> None:
        required = {
            "candidate-evidence-not-calibrated-detection",
            "identical-raw-iq-window-for-every-pattern",
            "known-pattern-qam-at-independent-pattern-winner",
            "no-lnb-label-center-or-receiver-correction",
            "retro-and-j1-are-conditioned-numerical-canaries-only",
        }
        expected_inputs = (
            self.window.recording_identity_digest,
            self.window.source_recording_ref.digest,
            self.window.source_window_ref.digest,
        )
        expected_dependencies = (
            self.search_algorithm_ref.digest,
            self.scorer_algorithm_ref.digest,
            self.qam_algorithm_ref.digest,
            self.config_ref.digest,
            *(item.template_ref.digest for item in self.pattern_qam),
            *(item.control_template_ref.digest for item in self.pattern_qam),
        )
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_6)
            or not self.analysis_id.startswith("slcfoqam6_")
            or self.config_ref.digest != self.search_receipt.plan.digest
            or tuple(item.pattern_index for item in self.pattern_qam)
            != tuple(range(len(self.search_receipt.patterns)))
            or len(self.pattern_qam) != len(self.search_receipt.patterns)
            or any(
                evidence.template_ref.digest != pattern.template_digest
                or evidence.role is not pattern.role
                or evidence.winner != winner
                for evidence, pattern, winner in zip(
                    self.pattern_qam,
                    self.search_receipt.patterns,
                    self.search_receipt.winners,
                    strict=True,
                )
            )
            or self.provenance.normalized_config_digest != self.config_ref.digest
            or self.provenance.input_digests != expected_inputs
            or self.provenance.dependency_digests != expected_dependencies
            or not self.candidates_only
            or self.calibrated_detection_count is not None
            or not required <= set(self.disclosures)
        ):
            raise ValueError("receiver-agnostic CFO/QAM window bundle is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(self.analysis_id, self.digest, self.schema)
