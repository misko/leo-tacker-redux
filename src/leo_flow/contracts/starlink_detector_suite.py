"""Versioned candidate evidence for the complete Starlink detector suite."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_token, require_utc_ns
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
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge

V0_2 = SchemaVersion(0, 2)


class StarlinkDetectorMethod(str, Enum):
    ANCHOR_8 = "anchor-8"
    DIFFERENTIAL_16 = "differential-16"
    DIFFERENTIAL_32 = "differential-32"
    GLRT_32 = "glrt-32"
    GLRT_64 = "glrt-64"
    FULL_FRAME_ACQUIRE = "full-frame-acquire"
    FULL_FRAME_VERIFY = "full-frame-verify"
    FULL_FRAME_FULL = "full-frame-full"


REPORT_METHOD_ORDER = tuple(StarlinkDetectorMethod)


class StarlinkSearchMode(str, Enum):
    SEARCHED_EXACT = "searched-exact"
    CONDITIONED_ON_ACQUIRE_WINNER = "conditioned-on-acquire-winner"


class StarlinkSamplingStratum(str, Enum):
    FULL_PILOT_BAND = "full-pilot-band"
    CLIPPED_PILOT_BAND = "clipped-pilot-band"


@dataclass(frozen=True)
class StarlinkFrameScoreSummaryV0_2:
    """Bounded summary; frame amplitudes were never coherently combined."""

    mean_score: float
    maximum_score: float
    support: int

    def __post_init__(self) -> None:
        require_finite(self.mean_score, "mean_score")
        require_finite(self.maximum_score, "maximum_score")
        if not 0 <= self.mean_score <= 1 or not 0 <= self.maximum_score <= 1:
            raise ValueError("normalized frame scores must lie in [0, 1]")
        if isinstance(self.support, bool) or not isinstance(self.support, int):
            raise TypeError("frame support must be an integer")
        if self.support < 0:
            raise ValueError("frame score summary support cannot be negative")
        if self.maximum_score + 1e-12 < self.mean_score:
            raise ValueError("maximum frame score cannot be below its mean")


@dataclass(frozen=True)
class StarlinkDetectorMethodEvidenceV0_2:
    """One report method, with selection and same-cell control made explicit."""

    schema: SchemaRef
    method: StarlinkDetectorMethod
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    exact_template_ref: ArtifactRef
    conditioned_control_template_ref: ArtifactRef
    search_identity_digest: Digest
    search_mode: StarlinkSearchMode
    selection_method: StarlinkDetectorMethod
    effective_search_cell_count: int
    winning_epoch_sample: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    reported_score: float
    conditioned_exact_score: float
    conditioned_control_score: float
    exact_minus_control_margin: float
    exact_frames: StarlinkFrameScoreSummaryV0_2
    control_frames: StarlinkFrameScoreSummaryV0_2
    pilot_symbol_indices: tuple[int, ...]
    symbol_set_role: str
    symbol_split_digest: Digest | None
    control_conditioning: str
    candidate_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-detector-method-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported Starlink detector evidence schema")
        if self.search_mode is StarlinkSearchMode.SEARCHED_EXACT:
            if self.selection_method is not self.method:
                raise ValueError("searched evidence must select itself")
        elif self.selection_method is not StarlinkDetectorMethod.FULL_FRAME_ACQUIRE:
            raise ValueError("conditioned full-frame evidence must cite acquire")
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
            raise ValueError("winning epoch sample must be a non-negative integer")
        for name in (
            "winning_coarse_cfo_hz",
            "winning_residual_cfo_hz",
            "reported_score",
            "conditioned_exact_score",
            "conditioned_control_score",
            "exact_minus_control_margin",
        ):
            require_finite(getattr(self, name), name)
        for name in (
            "reported_score",
            "conditioned_exact_score",
            "conditioned_control_score",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0, 1]")
        if abs(self.reported_score - self.conditioned_exact_score) > 1e-12:
            raise ValueError("reported score must reproduce when conditioned")
        if (
            abs(
                self.exact_minus_control_margin
                - (self.conditioned_exact_score - self.conditioned_control_score)
            )
            > 1e-12
        ):
            raise ValueError("exact/control margin is inconsistent")
        if self.exact_frames.support <= 0 or self.control_frames.support <= 0:
            raise ValueError("method evidence requires exact and control frame support")
        if self.exact_frames.support != self.control_frames.support:
            raise ValueError("exact/control frame support must match")
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
            raise ValueError("unknown pilot symbol-set role")
        if self.symbol_set_role in ("acquire", "verify", "full"):
            if self.symbol_split_digest is None:
                raise ValueError("full-frame blocks must cite their symbol split")
        elif self.symbol_split_digest is not None:
            raise ValueError("relative-phase methods cannot cite a full-frame split")
        if (
            self.control_conditioning
            != "exact-winning-epoch-coarse-and-residual-cfo-fixed"
        ):
            raise ValueError("roll control must be conditioned at the exact winner")
        if not self.candidate_only:
            raise ValueError("detector-suite evidence cannot emit a verdict")
        if "whole-search-calibration-required" not in self.reason_codes:
            raise ValueError(
                "candidate evidence must state its calibration requirement"
            )


@dataclass(frozen=True)
class StarlinkPssSssAcquisitionEvidenceV0_2:
    """Supporting lag-Doppler evidence from one pinned PSS+SSS replica."""

    schema: SchemaRef
    template_ref: ArtifactRef
    search_identity_digest: Digest
    search_cell_count: int
    winning_epoch_sample: int
    winning_doppler_hz: float
    searched_score: float
    conditioned_score: float
    frame_support: int
    captured_template_energy_fraction: float
    supporting_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-pss-sss-acquisition-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported PSS+SSS acquisition evidence schema")
        if self.search_cell_count <= 0 or self.frame_support <= 0:
            raise ValueError("PSS+SSS evidence requires searched cells and support")
        if self.winning_epoch_sample < 0:
            raise ValueError("PSS+SSS winning epoch must be non-negative")
        for name in (
            "winning_doppler_hz",
            "searched_score",
            "conditioned_score",
            "captured_template_energy_fraction",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.searched_score <= 1 or not 0 <= self.conditioned_score <= 1:
            raise ValueError("normalized PSS+SSS scores must lie in [0, 1]")
        if abs(self.searched_score - self.conditioned_score) > 1e-12:
            raise ValueError("PSS+SSS winner must reproduce when conditioned")
        if not 0 < self.captured_template_energy_fraction <= 1:
            raise ValueError("captured template energy fraction must lie in (0, 1]")
        if not self.supporting_only:
            raise ValueError("PSS+SSS evidence cannot decide edge-pilot presence")
        if not {"supporting-acquisition-only", "not-edge-pilot-detection"} <= set(
            self.reason_codes
        ):
            raise ValueError("PSS+SSS evidence must state its supporting-only role")


@dataclass(frozen=True)
class StarlinkDetectorSuiteBundleV0_2:
    """All eight report methods for one immutable receiver stream."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    sampling_stratum: StarlinkSamplingStratum
    suite_identity_digest: Digest
    symbol_split_digest: Digest
    methods: tuple[StarlinkDetectorMethodEvidenceV0_2, ...]
    pss_sss_acquisition: StarlinkPssSssAcquisitionEvidenceV0_2 | None
    provenance: Provenance
    candidates_only: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-detector-suite-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported Starlink detector suite schema")
        require_token(self.analysis_id, "analysis_id")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("suite input dimensions must be positive")
        if tuple(item.method for item in self.methods) != REPORT_METHOD_ORDER:
            raise ValueError("suite must contain every report method in frozen order")
        if len({item.method for item in self.methods}) != len(self.methods):
            raise ValueError("suite contains duplicate methods")
        if any(
            item.symbol_split_digest not in (None, self.symbol_split_digest)
            for item in self.methods
        ):
            raise ValueError("method cites another full-frame symbol split")
        if not self.candidates_only:
            raise ValueError("detector suite cannot emit detection verdicts")
        clipped = self.sample_rate_hz < 1_875_000.0
        if clipped != (
            self.sampling_stratum is StarlinkSamplingStratum.CLIPPED_PILOT_BAND
        ):
            raise ValueError("sampling stratum conflicts with pilot-band width")
        if (
            clipped
            and "clipped-pilot-band-not-calibration-compatible" not in self.warnings
        ):
            raise ValueError("clipped pilot analysis must carry its separation warning")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.analysis_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_2),
        )


@dataclass(frozen=True)
class StarlinkMultiRadioCandidateEvidenceV0_2:
    """Noncoherent candidate corroboration; never a detection or sync claim."""

    schema: SchemaRef
    evidence_id: str
    channel_number: int
    edge: StarlinkEdge
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    maximum_observed_first_sample_skew_ns: int
    maximum_cfo_span_hz: float
    radio_ids: tuple[RadioId, ...]
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    suite_refs: tuple[ArtifactRef, ...]
    coincidence_basis: str
    phase_combination: str
    candidate_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-multi-radio-candidate-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported multi-radio candidate evidence schema")
        require_token(self.evidence_id, "evidence_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("channel number must be one of 1, 2, 3, 4")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("candidate interval must be non-empty")
        if self.maximum_observed_first_sample_skew_ns < 0:
            raise ValueError("observed first-sample skew must be non-negative")
        require_finite(self.maximum_cfo_span_hz, "maximum_cfo_span_hz")
        if self.maximum_cfo_span_hz < 0:
            raise ValueError("CFO span must be non-negative")
        if (
            len(self.radio_ids) < 2
            or tuple(sorted(set(self.radio_ids))) != self.radio_ids
        ):
            raise ValueError("multi-radio evidence requires sorted unique radios")
        if tuple(sorted(set(self.receiver_chain_ids))) != self.receiver_chain_ids:
            raise ValueError("receiver identities must be sorted and unique")
        if len(self.receiver_chain_ids) != len(self.radio_ids):
            raise ValueError("one distinct receiver identity is required per radio")
        if len(self.suite_refs) != len(self.radio_ids):
            raise ValueError("one suite reference is required per radio")
        if self.coincidence_basis != "software-coordinated-multi-radio":
            raise ValueError("candidate evidence cannot claim hardware synchronization")
        if self.phase_combination != "none-noncoherent-evidence-only":
            raise ValueError("software-coordinated radios cannot be phase-combined")
        if not self.candidate_only:
            raise ValueError("uncalibrated coincidence cannot emit a detection")
        if "calibrated-stream-decisions-required-for-event" not in self.reason_codes:
            raise ValueError("candidate coincidence must defer to calibrated events")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
