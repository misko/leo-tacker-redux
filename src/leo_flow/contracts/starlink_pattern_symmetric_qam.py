"""Additive pattern-symmetric adaptive QAM evidence; v0.4 stays immutable."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_finite
from .core import (
    Digest,
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

V0_5 = SchemaVersion(0, 5)


@dataclass(frozen=True)
class PatternSymmetricQamPolicyV0_5:
    qam_window_sample_count: int = 50_000
    maximum_windows_per_stream: int = 3
    maximum_patterns: int = 9
    maximum_receivers: int = 2
    maximum_acquisition_runs: int = 54
    window_selection: str = "data-independent-uniform-declared-window-membership"
    control_pairing: str = "same-pattern-frozen-17-symbol-roll"

    def __post_init__(self) -> None:
        for name in (
            "qam_window_sample_count",
            "maximum_windows_per_stream",
            "maximum_patterns",
            "maximum_receivers",
            "maximum_acquisition_runs",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if (
            self.maximum_patterns > 33
            or self.maximum_receivers > 8
            or self.maximum_acquisition_runs
            < self.maximum_windows_per_stream
            * self.maximum_patterns
            * self.maximum_receivers
            or self.window_selection
            != "data-independent-uniform-declared-window-membership"
            or self.control_pairing != "same-pattern-frozen-17-symbol-roll"
        ):
            raise ValueError("pattern-symmetric QAM policy is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class PatternQamWindowEvidenceV0_5:
    source_window_index: int
    start_sample: int
    stop_sample: int
    search_identity_digest: Digest
    algorithm_digest: Digest
    config_digest: Digest
    coarse_search_cell_count: int
    refinement_search_cell_count: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    complete_frame_count: int
    hard_symbol_accuracy: float
    rms_evm: float
    qam_goodness: float

    def __post_init__(self) -> None:
        for name in (
            "winning_cfo_hz",
            "hard_symbol_accuracy",
            "rms_evm",
            "qam_goodness",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.source_window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
            or self.winning_epoch_sample < 0
            or self.complete_frame_count <= 0
            or self.coarse_search_cell_count <= 0
            or self.refinement_search_cell_count <= 0
            or not 0 <= self.hard_symbol_accuracy <= 1
            or self.rms_evm < 0
            or not 0 <= self.qam_goodness <= 1
        ):
            raise ValueError("pattern QAM window evidence is invalid")


@dataclass(frozen=True)
class PatternQamEvidenceV0_5:
    pattern_index: int
    role: AdaptivePatternRole
    template_digest: Digest
    control_template_digest: Digest
    windows: tuple[PatternQamWindowEvidenceV0_5, ...]

    def __post_init__(self) -> None:
        expected = (
            AdaptivePatternRole.QIN
            if self.pattern_index == 0
            else AdaptivePatternRole.SURROGATE
        )
        if (
            self.pattern_index < 0
            or self.role is not expected
            or not self.windows
            or tuple(item.source_window_index for item in self.windows)
            != tuple(sorted({item.source_window_index for item in self.windows}))
        ):
            raise ValueError("pattern QAM evidence is noncanonical")


@dataclass(frozen=True)
class PatternSymmetricQamStreamV0_5:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    patterns: tuple[PatternQamEvidenceV0_5, ...]

    def __post_init__(self) -> None:
        if (
            self.sample_rate_hz <= 0
            or not self.patterns
            or tuple(item.pattern_index for item in self.patterns)
            != tuple(range(len(self.patterns)))
            or len(
                {
                    tuple((w.start_sample, w.stop_sample) for w in p.windows)
                    for p in self.patterns
                }
            )
            != 1
        ):
            raise ValueError("pattern-symmetric stream membership differs")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            str(self.radio_id),
            str(self.segment_id),
            str(self.receiver_chain_id),
            str(self.edge),
        )


@dataclass(frozen=True)
class PatternSymmetricAdaptiveQamBundleV0_5:
    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_adaptive_response_digest: Digest
    policy: PatternSymmetricQamPolicyV0_5
    pattern_template_digests: tuple[Digest, ...]
    streams: tuple[PatternSymmetricQamStreamV0_5, ...]
    acquisition_run_count: int
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.pattern-symmetric-adaptive-qam-bundle"

    def __post_init__(self) -> None:
        required = {
            "identical-data-independent-windows-for-every-pattern",
            "identical-epoch-cfo-acquisition-for-every-pattern",
            "known-pattern-qam-not-detection",
            "retro-and-j1-are-conditioned-numerical-canaries-only",
        }
        expected_runs = sum(
            len(pattern.windows)
            for stream in self.streams
            for pattern in stream.patterns
        )
        search_geometries = {
            (
                window.algorithm_digest,
                window.config_digest,
                window.coarse_search_cell_count,
                window.refinement_search_cell_count,
            )
            for stream in self.streams
            for pattern in stream.patterns
            for window in pattern.windows
        }
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_5)
            or not self.analysis_id.startswith("slpsqam5_")
            or not self.pattern_template_digests
            or tuple(item.identity for item in self.streams)
            != tuple(sorted(item.identity for item in self.streams))
            or any(
                tuple(item.template_digest for item in stream.patterns)
                != self.pattern_template_digests
                for stream in self.streams
            )
            or self.acquisition_run_count != expected_runs
            or expected_runs > self.policy.maximum_acquisition_runs
            or len(search_geometries) != 1
            or not self.candidate_only
            or self.calibrated_detection_count is not None
            or not required <= set(self.warnings)
        ):
            raise ValueError("pattern-symmetric adaptive QAM bundle is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
