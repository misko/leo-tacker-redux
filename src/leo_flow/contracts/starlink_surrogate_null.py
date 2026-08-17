"""Paired deterministic-surrogate evidence for Starlink pilot searches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_positive, require_token
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
from .starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
    StarlinkFrameScoreSummaryV0_2,
)

V0_1 = SchemaVersion(0, 1)
MINIMUM_DEFAULT_SURROGATES = 4
MAXIMUM_SURROGATES = 32


class StarlinkSearchPatternRole(str, Enum):
    QIN_EXACT = "qin-exact"
    PRECOMMITTED_SURROGATE = "precommitted-surrogate"


class StarlinkPatternSearchMode(str, Enum):
    SEARCHED = "searched"
    CONDITIONED_ON_PATTERN_ACQUIRE_WINNER = "conditioned-on-pattern-acquire-winner"


@dataclass(frozen=True)
class StarlinkSearchGridV0_1:
    """Self-contained frozen v0.2 grid and resource-bound snapshot."""

    config_ref: ArtifactRef
    epoch_hypotheses_samples: tuple[int, ...]
    coarse_cfo_hypotheses_hz: tuple[float, ...]
    glrt_residual_cfo_hypotheses_hz: tuple[float, ...]
    acquire_symbols: tuple[int, ...]
    verify_symbols: tuple[int, ...]
    maximum_probe_samples: int
    maximum_outer_search_cells: int
    maximum_effective_search_cells: int
    maximum_frame_summaries: int

    def __post_init__(self) -> None:
        if (
            not self.epoch_hypotheses_samples
            or len(set(self.epoch_hypotheses_samples))
            != len(self.epoch_hypotheses_samples)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.epoch_hypotheses_samples
            )
        ):
            raise ValueError("epoch hypotheses must be unique and non-negative")
        for values, label in (
            (self.coarse_cfo_hypotheses_hz, "coarse CFO hypotheses"),
            (self.glrt_residual_cfo_hypotheses_hz, "residual CFO hypotheses"),
        ):
            if not values or len(set(values)) != len(values):
                raise ValueError(f"{label} must be non-empty and unique")
            for value in values:
                require_finite(value, label)
        if set(self.acquire_symbols) & set(self.verify_symbols):
            raise ValueError("acquire and verify symbols must be disjoint")
        if tuple(sorted(self.acquire_symbols + self.verify_symbols)) != tuple(
            range(2, 302)
        ):
            raise ValueError("acquire and verify must partition all pilot symbols")
        for name in (
            "maximum_probe_samples",
            "maximum_outer_search_cells",
            "maximum_effective_search_cells",
            "maximum_frame_summaries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.outer_search_cell_count > self.maximum_outer_search_cells:
            raise ValueError("outer search exceeds its resource bound")
        if self.glrt_search_cell_count > self.maximum_effective_search_cells:
            raise ValueError("GLRT search exceeds its resource bound")

    @property
    def outer_search_cell_count(self) -> int:
        return len(self.epoch_hypotheses_samples) * len(self.coarse_cfo_hypotheses_hz)

    @property
    def glrt_search_cell_count(self) -> int:
        return self.outer_search_cell_count * len(self.glrt_residual_cfo_hypotheses_hz)

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSearchPatternV0_1:
    """Identity of one exact or precommitted QPSK edge-pilot pattern."""

    schema: SchemaRef
    pattern_id: str
    role: StarlinkSearchPatternRole
    template_ref: ArtifactRef
    edge: StarlinkEdge
    pilot_subcarrier_indices: tuple[int, ...]
    first_pilot_symbol: int
    last_pilot_symbol: int
    frame_rate_hz: float
    sample_rate_hz: float
    frame_sample_count: int
    template_energy: float
    qpsk_state_matrix_digest: Digest
    generator_id: str
    generator_seed: int | None
    codebook_index: int | None
    data_independent: bool

    SCHEMA_ID = "org.leo-flow.starlink-search-pattern"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink search-pattern schema")
        require_token(self.pattern_id, "pattern_id")
        expected_indices = (
            tuple(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else tuple(range(488, 496))
        )
        if self.pilot_subcarrier_indices != expected_indices:
            raise ValueError("pattern must preserve the eight edge pilot bins")
        if (self.first_pilot_symbol, self.last_pilot_symbol) != (2, 301):
            raise ValueError("pattern must preserve all 300 pilot symbols")
        require_positive(self.frame_rate_hz, "frame_rate_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if (
            isinstance(self.frame_sample_count, bool)
            or not isinstance(self.frame_sample_count, int)
            or self.frame_sample_count <= 0
        ):
            raise ValueError("frame_sample_count must be a positive integer")
        require_positive(self.template_energy, "template_energy")
        require_token(self.generator_id, "generator_id")
        if self.role is StarlinkSearchPatternRole.QIN_EXACT:
            if self.generator_seed is not None or self.codebook_index is not None:
                raise ValueError("Qin exact pattern cannot claim surrogate seed/index")
        else:
            for value, label in (
                (self.generator_seed, "generator_seed"),
                (self.codebook_index, "codebook_index"),
            ):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"surrogate {label} must be non-negative")
            if self.codebook_index >= MAXIMUM_SURROGATES:  # type: ignore[operator]
                raise ValueError("surrogate codebook index exceeds its bound")
        if not self.data_independent:
            raise ValueError("search patterns must be generated independently of data")


@dataclass(frozen=True)
class StarlinkPatternMethodEvidenceV0_1:
    """One method's independently maximized score for one pattern."""

    schema: SchemaRef
    method: StarlinkDetectorMethod
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    input_digest: Digest
    pattern: StarlinkSearchPatternV0_1
    search_plan_digest: Digest
    search_identity_digest: Digest
    search_mode: StarlinkPatternSearchMode
    selection_method: StarlinkDetectorMethod
    effective_search_cell_count: int
    winning_epoch_sample: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    score: float
    frames: StarlinkFrameScoreSummaryV0_2
    pilot_symbol_indices: tuple[int, ...]
    symbol_set_role: str
    symbol_split_digest: Digest | None

    SCHEMA_ID = "org.leo-flow.starlink-pattern-method-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported pattern-method evidence schema")
        if self.search_mode is StarlinkPatternSearchMode.SEARCHED:
            if self.selection_method is not self.method:
                raise ValueError("searched evidence must select itself")
        elif self.selection_method is not StarlinkDetectorMethod.FULL_FRAME_ACQUIRE:
            raise ValueError("conditioned evidence must cite its pattern acquire")
        if (
            isinstance(self.effective_search_cell_count, bool)
            or not isinstance(self.effective_search_cell_count, int)
            or self.effective_search_cell_count <= 0
        ):
            raise ValueError("effective search cell count must be positive")
        if (
            isinstance(self.winning_epoch_sample, bool)
            or not isinstance(self.winning_epoch_sample, int)
            or self.winning_epoch_sample < 0
        ):
            raise ValueError("winning epoch must be non-negative")
        for name in (
            "winning_coarse_cfo_hz",
            "winning_residual_cfo_hz",
            "score",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.score <= 1:
            raise ValueError("pattern score must lie in [0,1]")
        if self.frames.support <= 0:
            raise ValueError("pattern evidence requires frame support")
        if (
            not self.pilot_symbol_indices
            or tuple(sorted(set(self.pilot_symbol_indices)))
            != self.pilot_symbol_indices
            or self.pilot_symbol_indices[0] < 2
            or self.pilot_symbol_indices[-1] > 301
        ):
            raise ValueError("pilot symbols must be a sorted subset of 2..301")
        if self.symbol_set_role not in (
            "anchor",
            "contiguous",
            "acquire",
            "verify",
            "full",
        ):
            raise ValueError("unknown symbol-set role")
        if self.symbol_set_role in ("acquire", "verify", "full"):
            if self.symbol_split_digest is None:
                raise ValueError("full-frame method must cite its symbol split")
        elif self.symbol_split_digest is not None:
            raise ValueError("relative method cannot cite a full-frame split")


@dataclass(frozen=True)
class StarlinkPatternDetectionV0_1:
    """All eight report methods from one invocation of the detector port."""

    schema: SchemaRef
    detection_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    input_digest: Digest
    search_grid: StarlinkSearchGridV0_1
    pattern: StarlinkSearchPatternV0_1
    methods: tuple[StarlinkPatternMethodEvidenceV0_1, ...]
    provenance: Provenance
    candidate_only: bool

    SCHEMA_ID = "org.leo-flow.starlink-pattern-detection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported pattern-detection schema")
        require_token(self.detection_id, "detection_id")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if self.probe_sample_count <= 0:
            raise ValueError("probe_sample_count must be positive")
        if (
            self.pattern.edge is not self.edge
            or self.pattern.sample_rate_hz != self.sample_rate_hz
        ):
            raise ValueError("pattern dimensions do not match detector input")
        if tuple(item.method for item in self.methods) != REPORT_METHOD_ORDER:
            raise ValueError("pattern detection must cover all eight report methods")
        if any(
            item.pattern != self.pattern
            or item.input_digest != self.input_digest
            or item.config_ref != self.search_grid.config_ref
            for item in self.methods
        ):
            raise ValueError(
                "method evidence belongs to another input, pattern or grid"
            )
        if not self.candidate_only:
            raise ValueError("uncalibrated pattern searches cannot emit a verdict")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkPairedMethodNullV0_1:
    """Finite paired-surrogate score sample for one report method."""

    method: StarlinkDetectorMethod
    target_score: float
    surrogate_scores: tuple[float, ...]
    empirical_upper_tail_probability: float

    def __post_init__(self) -> None:
        require_finite(self.target_score, "target_score")
        if not 0 <= self.target_score <= 1:
            raise ValueError("target score must lie in [0,1]")
        if not 1 <= len(self.surrogate_scores) <= MAXIMUM_SURROGATES:
            raise ValueError("surrogate score count must lie in [1,32]")
        if any(not 0 <= score <= 1 for score in self.surrogate_scores):
            raise ValueError("surrogate scores must lie in [0,1]")
        expected = (
            1 + sum(score >= self.target_score for score in self.surrogate_scores)
        ) / (len(self.surrogate_scores) + 1)
        require_finite(
            self.empirical_upper_tail_probability,
            "empirical_upper_tail_probability",
        )
        if abs(self.empirical_upper_tail_probability - expected) > 1e-12:
            raise ValueError("paired empirical upper-tail probability is inconsistent")


@dataclass(frozen=True)
class StarlinkPairedSurrogateEvidenceV0_1:
    """Qin search paired with independently searched deterministic surrogates."""

    schema: SchemaRef
    analysis_id: str
    exact: StarlinkPatternDetectionV0_1
    surrogates: tuple[StarlinkPatternDetectionV0_1, ...]
    method_nulls: tuple[StarlinkPairedMethodNullV0_1, ...]
    codebook_digest: Digest
    minimum_recommended_surrogates: int
    candidate_only: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-paired-surrogate-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported paired-surrogate evidence schema")
        require_token(self.analysis_id, "analysis_id")
        if self.exact.pattern.role is not StarlinkSearchPatternRole.QIN_EXACT:
            raise ValueError("paired evidence target must be the exact Qin pattern")
        if not 1 <= len(self.surrogates) <= MAXIMUM_SURROGATES:
            raise ValueError("paired evidence requires 1..32 surrogates")
        if any(
            item.pattern.role is not StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE
            for item in self.surrogates
        ):
            raise ValueError("null members must be precommitted surrogates")
        indices = tuple(item.pattern.codebook_index for item in self.surrogates)
        refs = tuple(item.pattern.template_ref for item in self.surrogates)
        seeds = tuple(item.pattern.generator_seed for item in self.surrogates)
        states = tuple(
            item.pattern.qpsk_state_matrix_digest for item in self.surrogates
        )
        if any(
            len(set(values)) != len(values) for values in (indices, refs, seeds, states)
        ):
            raise ValueError("surrogate patterns must be distinct")
        if self.exact.pattern.template_ref in refs:
            raise ValueError("surrogate pattern cannot equal the exact Qin template")
        if tuple(item.method for item in self.method_nulls) != REPORT_METHOD_ORDER:
            raise ValueError("paired nulls must cover all report methods")
        exact_by_method = {item.method: item for item in self.exact.methods}
        for null in self.method_nulls:
            target = exact_by_method[null.method]
            controls = tuple(
                next(method for method in item.methods if method.method is null.method)
                for item in self.surrogates
            )
            if null.target_score != target.score or null.surrogate_scores != tuple(
                item.score for item in controls
            ):
                raise ValueError("paired null scores do not reproduce method evidence")
            if any(
                item.search_plan_digest != target.search_plan_digest
                or item.effective_search_cell_count
                != target.effective_search_cell_count
                or item.search_mode is not target.search_mode
                or item.selection_method is not target.selection_method
                or item.pilot_symbol_indices != target.pilot_symbol_indices
                for item in controls
            ):
                raise ValueError(
                    "target and surrogates must use the identical search plan"
                )
        identities = (
            self.exact.recording_id,
            self.exact.recording_identity_digest,
            self.exact.segment_id,
            self.exact.receiver_chain_id,
            self.exact.input_digest,
        )
        if any(
            (
                item.recording_id,
                item.recording_identity_digest,
                item.segment_id,
                item.receiver_chain_id,
                item.input_digest,
            )
            != identities
            or item.search_grid != self.exact.search_grid
            for item in self.surrogates
        ):
            raise ValueError("paired searches must share one immutable input and grid")
        if self.minimum_recommended_surrogates != MINIMUM_DEFAULT_SURROGATES:
            raise ValueError("v0.1 recommendation is fixed at four surrogates")
        if not self.candidate_only:
            raise ValueError("paired surrogate evidence cannot emit a verdict")
        required = {
            "finite-paired-surrogate-controls",
            "not-verified-signal-absent",
            "not-calibrated-detection",
        }
        if not required <= set(self.warnings):
            raise ValueError("paired evidence must disclose null limitations")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
