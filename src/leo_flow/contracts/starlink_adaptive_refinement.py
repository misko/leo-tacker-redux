"""Versioned contracts for legacy-style adaptive Starlink time refinement.

The prompt timeline selects power seeds without looking at Qin.  This contract
adds fixed sentinel probes and a second, pattern-symmetric local expansion.  A
Qin pattern and every precommitted surrogate receive the same candidate quota;
the union of their local windows is then searched by every pattern.  This keeps
the time look-elsewhere operation observable instead of giving Qin a privileged
set of windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite
from .core import ArtifactRef, Digest, SchemaRef, SchemaVersion, canonical_digest

V0_1 = SchemaVersion(0, 1)
MAXIMUM_ADAPTIVE_PATTERNS = 33
MAXIMUM_ADAPTIVE_BASE_WINDOWS = 128
MAXIMUM_ADAPTIVE_WINDOWS = 512


class AdaptiveWindowStage(str, Enum):
    BASE = "base-sentinel-or-power-seed"
    LOCAL = "pattern-symmetric-local-follow-up"


@dataclass(frozen=True)
class StarlinkAdaptiveRefinementPlanV0_1:
    """Frozen two-pass time-search geometry, expressed in exact samples."""

    probe_sample_count: int
    sentinel_stride_samples: int
    local_radius_samples: int
    local_stride_samples: int
    candidate_centers_per_pattern: int
    maximum_power_seeds: int
    maximum_base_windows: int = MAXIMUM_ADAPTIVE_BASE_WINDOWS
    maximum_exact_windows: int = MAXIMUM_ADAPTIVE_WINDOWS
    base_selection: str = "fixed-sentinels-plus-pattern-blind-power-seeds"
    follow_up_selection: str = (
        "equal-top-score-quota-per-pattern-then-search-pattern-union"
    )

    def __post_init__(self) -> None:
        for name in (
            "probe_sample_count",
            "sentinel_stride_samples",
            "local_radius_samples",
            "local_stride_samples",
            "candidate_centers_per_pattern",
            "maximum_power_seeds",
            "maximum_base_windows",
            "maximum_exact_windows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.local_stride_samples > self.local_radius_samples:
            raise ValueError("local stride cannot exceed the follow-up radius")
        if self.maximum_base_windows > MAXIMUM_ADAPTIVE_BASE_WINDOWS:
            raise ValueError("adaptive base-window bound is too large")
        if (
            not self.maximum_base_windows
            <= self.maximum_exact_windows
            <= (MAXIMUM_ADAPTIVE_WINDOWS)
        ):
            raise ValueError("adaptive exact-window bound is invalid")
        if self.base_selection != ("fixed-sentinels-plus-pattern-blind-power-seeds"):
            raise ValueError("unsupported adaptive base selection")
        if self.follow_up_selection != (
            "equal-top-score-quota-per-pattern-then-search-pattern-union"
        ):
            raise ValueError("unsupported adaptive follow-up selection")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkAdaptiveBaseWindowV0_1:
    start_sample: int
    stop_sample: int
    selection_reasons: tuple[str, ...]
    power_seed_ranks: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.stop_sample <= self.start_sample:
            raise ValueError("adaptive base window geometry is invalid")
        if not self.selection_reasons or self.selection_reasons != tuple(
            sorted(set(self.selection_reasons))
        ):
            raise ValueError("adaptive base reasons must be canonical")
        if self.power_seed_ranks != tuple(sorted(set(self.power_seed_ranks))):
            raise ValueError("adaptive power ranks must be canonical")
        if any(rank < 0 for rank in self.power_seed_ranks):
            raise ValueError("adaptive power rank cannot be negative")


@dataclass(frozen=True)
class StarlinkAdaptivePatternScoreV0_1:
    pattern_ref: ArtifactRef
    start_sample: int
    score: float

    def __post_init__(self) -> None:
        if self.start_sample < 0:
            raise ValueError("adaptive pattern score start cannot be negative")
        require_finite(self.score, "score")
        if not 0 <= self.score <= 1:
            raise ValueError("adaptive pattern score must lie in [0,1]")


@dataclass(frozen=True)
class StarlinkAdaptiveExactWindowV0_1:
    window_index: int
    stage: AdaptiveWindowStage
    start_sample: int
    stop_sample: int
    base_selection_reasons: tuple[str, ...]
    selected_by_pattern_refs: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("adaptive exact window geometry is invalid")
        if self.base_selection_reasons != tuple(
            sorted(set(self.base_selection_reasons))
        ):
            raise ValueError("adaptive exact reasons must be canonical")
        identities = tuple(str(item.digest) for item in self.selected_by_pattern_refs)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("adaptive selecting patterns must be canonical")
        if self.stage is AdaptiveWindowStage.BASE:
            if not self.base_selection_reasons or self.selected_by_pattern_refs:
                raise ValueError("base window provenance is inconsistent")
        elif not self.selected_by_pattern_refs:
            raise ValueError("local window must identify its selecting patterns")


@dataclass(frozen=True)
class StarlinkAdaptiveRefinementSelectionV0_1:
    schema: SchemaRef
    segment_sample_count: int
    plan: StarlinkAdaptiveRefinementPlanV0_1
    pattern_refs: tuple[ArtifactRef, ...]
    base_windows: tuple[StarlinkAdaptiveBaseWindowV0_1, ...]
    exact_windows: tuple[StarlinkAdaptiveExactWindowV0_1, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-adaptive-refinement-selection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive refinement schema")
        if self.segment_sample_count < self.plan.probe_sample_count:
            raise ValueError("adaptive probe exceeds its segment")
        pattern_digests = tuple(str(item.digest) for item in self.pattern_refs)
        if (
            not pattern_digests
            or pattern_digests != tuple(sorted(set(pattern_digests)))
            or len(pattern_digests) > MAXIMUM_ADAPTIVE_PATTERNS
        ):
            raise ValueError("adaptive pattern identities are invalid")
        if not self.base_windows or len(self.base_windows) > (
            self.plan.maximum_base_windows
        ):
            raise ValueError("adaptive base-window membership is invalid")
        if not self.exact_windows or len(self.exact_windows) > (
            self.plan.maximum_exact_windows
        ):
            raise ValueError("adaptive exact-window membership is invalid")
        if tuple(item.window_index for item in self.exact_windows) != tuple(
            range(len(self.exact_windows))
        ):
            raise ValueError("adaptive exact-window indexes are noncanonical")
        starts = tuple(item.start_sample for item in self.exact_windows)
        if starts != tuple(sorted(set(starts))):
            raise ValueError("adaptive exact-window starts are noncanonical")
        if any(
            item.stop_sample > self.segment_sample_count
            or item.stop_sample - item.start_sample != self.plan.probe_sample_count
            for item in self.exact_windows
        ):
            raise ValueError("adaptive exact window exceeds declared geometry")
        required = {
            "candidate-evidence-not-calibrated-detection",
            "base-sentinels-span-dwell-but-do-not-cover-every-sample",
            "power-seeds-are-pattern-blind",
            "local-follow-up-uses-equal-quota-for-qin-and-surrogates",
            "all-patterns-search-the-union-of-selected-local-windows",
            "time-look-elsewhere-calibration-required",
        }
        if not required <= set(self.warnings):
            raise ValueError("adaptive refinement disclosures are incomplete")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
