"""Dense legacy-parity Starlink symbolwise replay evidence."""

from __future__ import annotations

import math
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
from .starlink_surrogate_null import (
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)

V0_1 = SchemaVersion(0, 1)
MAXIMUM_REPLAY_WINDOWS = 600
MAXIMUM_REPLAY_PATTERNS = 5


@dataclass(frozen=True)
class StarlinkReceiverFrequencyCenterV0_1:
    """Immutable receiver-path CFO center resolved outside the analyzer."""

    schema: SchemaRef
    calibration_id: str
    hardware_epoch_digest: Digest
    receiver_signal_path_digest: Digest
    source_ref: ArtifactRef
    center_cfo_hz: float
    reference_definition: str
    data_independent: bool

    SCHEMA_ID = "org.leo-flow.starlink-receiver-frequency-center"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported receiver frequency-center schema")
        require_token(self.calibration_id, "calibration_id")
        require_finite(self.center_cfo_hz, "center_cfo_hz")
        if self.reference_definition != "absolute-cfo-relative-to-recording-if-center":
            raise ValueError("unsupported receiver frequency-center reference")
        if not self.data_independent:
            raise ValueError("receiver frequency center must be fixed before replay")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.calibration_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkSymbolwisePatternEvidenceV0_1:
    """One pattern's complete legacy-style symbolwise search in one window."""

    pattern: StarlinkSearchPatternV0_1
    selection_control_template_ref: ArtifactRef
    timing_search_cell_count: int
    refinement_search_cell_count: int
    retained_candidate_count: int
    selected_candidate_rank: int
    winning_epoch_sample: int
    timing_coarse_cfo_hz: float
    timing_score: float
    timing_folded_median: float
    timing_peak_to_median: float
    timing_symbol_frame_support: int
    symbolwise_coarse_cfo_hz: float
    symbolwise_residual_cfo_hz: float
    winning_cfo_hz: float
    symbolwise_score: float
    symbolwise_control_score: float
    symbolwise_margin: float
    symbolwise_coherence: float
    symbolwise_control_coherence: float
    conditioned_score: float
    conditioned_control_score: float
    conditioned_margin: float
    conditioned_maximum_score: float
    conditioned_control_maximum_score: float
    frame_support: int
    symbol_match_count: int
    selection_score: float

    def __post_init__(self) -> None:
        for name in (
            "timing_search_cell_count",
            "refinement_search_cell_count",
            "retained_candidate_count",
            "timing_symbol_frame_support",
            "frame_support",
            "symbol_match_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("selected_candidate_rank", "winning_epoch_sample"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.selected_candidate_rank >= self.retained_candidate_count:
            raise ValueError("selected pattern candidate rank is outside retention")
        for name in (
            "timing_coarse_cfo_hz",
            "timing_score",
            "timing_folded_median",
            "timing_peak_to_median",
            "symbolwise_coarse_cfo_hz",
            "symbolwise_residual_cfo_hz",
            "winning_cfo_hz",
            "symbolwise_score",
            "symbolwise_control_score",
            "symbolwise_margin",
            "symbolwise_coherence",
            "symbolwise_control_coherence",
            "conditioned_score",
            "conditioned_control_score",
            "conditioned_margin",
            "conditioned_maximum_score",
            "conditioned_control_maximum_score",
            "selection_score",
        ):
            require_finite(getattr(self, name), name)
        for name in (
            "timing_score",
            "timing_folded_median",
            "symbolwise_score",
            "symbolwise_control_score",
            "symbolwise_coherence",
            "symbolwise_control_coherence",
            "conditioned_score",
            "conditioned_control_score",
            "conditioned_maximum_score",
            "conditioned_control_maximum_score",
            "selection_score",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0,1]")
        if self.timing_peak_to_median < 0:
            raise ValueError("timing peak-to-median ratio cannot be negative")
        if (
            abs(
                self.symbolwise_margin
                - (self.symbolwise_score - self.symbolwise_control_score)
            )
            > 1e-12
        ):
            raise ValueError("symbolwise target/control margin is inconsistent")
        if (
            abs(
                self.conditioned_margin
                - (self.conditioned_score - self.conditioned_control_score)
            )
            > 1e-12
        ):
            raise ValueError("conditioned target/control margin is inconsistent")


@dataclass(frozen=True)
class StarlinkSymbolwiseWindowEvidenceV0_1:
    """Canonical target and surrogate evidence for one exact dwell window."""

    window_index: int
    start_sample: int
    stop_sample: int
    input_digest: Digest
    patterns: tuple[StarlinkSymbolwisePatternEvidenceV0_1, ...]

    def __post_init__(self) -> None:
        for name in ("window_index", "start_sample"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.stop_sample <= self.start_sample:
            raise ValueError("symbolwise window must be non-empty")
        if not 2 <= len(self.patterns) <= MAXIMUM_REPLAY_PATTERNS:
            raise ValueError("symbolwise window needs Qin plus bounded surrogates")
        roles = tuple(item.pattern.role for item in self.patterns)
        if roles[0] is not StarlinkSearchPatternRole.QIN_EXACT or any(
            role is not StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE
            for role in roles[1:]
        ):
            raise ValueError("symbolwise patterns must be Qin then surrogates")
        indexes = tuple(item.pattern.codebook_index for item in self.patterns[1:])
        if indexes != tuple(range(len(indexes))):
            raise ValueError("symbolwise surrogate indexes must be precommitted")
        target = self.patterns[0]
        for item in self.patterns[1:]:
            if (
                item.timing_search_cell_count != target.timing_search_cell_count
                or item.refinement_search_cell_count
                != target.refinement_search_cell_count
            ):
                raise ValueError("target and surrogates must have identical searches")

    @property
    def sample_count(self) -> int:
        return self.stop_sample - self.start_sample

    @property
    def qin(self) -> StarlinkSymbolwisePatternEvidenceV0_1:
        return self.patterns[0]


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayBundleV0_1:
    """One receiver's fixed-cadence native replay over an immutable segment."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    frequency_center: StarlinkReceiverFrequencyCenterV0_1
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    surrogate_codebook_digest: Digest
    window_sample_count: int
    cadence_sample_count: int
    windows: tuple[StarlinkSymbolwiseWindowEvidenceV0_1, ...]
    analyzed_union_sample_count: int
    coverage_fraction: float
    timing_search_cell_count: int
    refinement_search_cell_count: int
    maximum_working_bytes: int
    provenance: Provenance
    candidates_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-symbolwise-replay"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported symbolwise replay schema")
        if not self.analysis_id.startswith("slsymreplay_"):
            raise ValueError("invalid symbolwise replay identity")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0:
            raise ValueError("symbolwise replay sample rate must be positive")
        for name in (
            "segment_sample_count",
            "window_sample_count",
            "cadence_sample_count",
            "analyzed_union_sample_count",
            "timing_search_cell_count",
            "refinement_search_cell_count",
            "maximum_working_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not self.window_sample_count <= self.cadence_sample_count:
            raise ValueError("v0.1 replay windows cannot overlap")
        if not self.windows or len(self.windows) > MAXIMUM_REPLAY_WINDOWS:
            raise ValueError("symbolwise replay window count is empty or unbounded")
        if tuple(item.window_index for item in self.windows) != tuple(
            range(len(self.windows))
        ):
            raise ValueError("symbolwise replay indexes must be canonical")
        expected_starts = tuple(
            index * self.cadence_sample_count for index in range(len(self.windows))
        )
        if tuple(item.start_sample for item in self.windows) != expected_starts:
            raise ValueError("symbolwise replay does not use the declared cadence")
        if any(
            item.sample_count != self.window_sample_count
            or item.stop_sample > self.segment_sample_count
            for item in self.windows
        ):
            raise ValueError("symbolwise replay window geometry is inconsistent")
        expected_union = len(self.windows) * self.window_sample_count
        if self.analyzed_union_sample_count != expected_union:
            raise ValueError("symbolwise replay union coverage is inconsistent")
        expected_fraction = expected_union / self.segment_sample_count
        if not math.isclose(self.coverage_fraction, expected_fraction, abs_tol=1e-15):
            raise ValueError("symbolwise replay coverage fraction is inconsistent")
        pattern_count = len(self.windows[0].patterns)
        if any(len(item.patterns) != pattern_count for item in self.windows):
            raise ValueError("symbolwise replay pattern membership drifted")
        baseline_patterns = tuple(
            (
                item.pattern,
                item.selection_control_template_ref,
                item.timing_search_cell_count,
                item.refinement_search_cell_count,
            )
            for item in self.windows[0].patterns
        )
        if any(
            tuple(
                (
                    item.pattern,
                    item.selection_control_template_ref,
                    item.timing_search_cell_count,
                    item.refinement_search_cell_count,
                )
                for item in window.patterns
            )
            != baseline_patterns
            for window in self.windows[1:]
        ):
            raise ValueError("symbolwise pattern identity or search changed by window")
        expected_codebook_digest = canonical_digest(
            tuple(item.pattern for item in self.windows[0].patterns)
        )
        if self.surrogate_codebook_digest != expected_codebook_digest:
            raise ValueError("symbolwise surrogate codebook digest is inconsistent")
        per_window_timing = sum(
            item.timing_search_cell_count for item in self.windows[0].patterns
        )
        per_window_refinement = sum(
            item.refinement_search_cell_count for item in self.windows[0].patterns
        )
        if self.timing_search_cell_count != per_window_timing * len(self.windows):
            raise ValueError("symbolwise timing resource count is inconsistent")
        if self.refinement_search_cell_count != per_window_refinement * len(
            self.windows
        ):
            raise ValueError("symbolwise refinement resource count is inconsistent")
        if self.provenance.normalized_config_digest != self.config_ref.digest:
            raise ValueError("symbolwise provenance uses another replay config")
        required_dependencies = {
            self.algorithm_ref.digest,
            self.frequency_center.digest,
            self.frequency_center.source_ref.digest,
            *(item.pattern.template_ref.digest for item in self.windows[0].patterns),
            *(
                item.selection_control_template_ref.digest
                for item in self.windows[0].patterns
            ),
        }
        if not required_dependencies <= set(self.provenance.dependency_digests):
            raise ValueError("symbolwise provenance omits a replay dependency")
        if not self.candidates_only:
            raise ValueError("uncalibrated replay cannot emit a detection")
        required = {
            "legacy-parity-evidence-not-runtime-dependency",
            "finite-pattern-controls-not-empirical-null",
            "whole-search-calibration-required",
            "known-pilot-not-user-payload",
            "conditioned-roll17-is-not-pattern-symmetric-null",
            "receiver-center-is-explicit-calibration-input",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("symbolwise replay omits required limitations")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_1),
        )
