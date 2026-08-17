"""Additive symmetric rolled-template search evidence for Starlink scores."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite
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


class StarlinkFullSearchControlMode(str, Enum):
    SEARCHED_ROLLED_TEMPLATE = "searched-rolled-template"
    CONDITIONED_ON_ROLLED_ACQUIRE_WINNER = "conditioned-on-rolled-acquire-winner"


@dataclass(frozen=True)
class StarlinkFullSearchControlMethodEvidenceV0_1:
    """One rolled-template statistic searched like its target counterpart."""

    schema: SchemaRef
    method: StarlinkDetectorMethod
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    rolled_template_ref: ArtifactRef
    search_identity_digest: Digest
    search_mode: StarlinkFullSearchControlMode
    selection_method: StarlinkDetectorMethod
    effective_search_cell_count: int
    winning_epoch_sample: int
    winning_coarse_cfo_hz: float
    winning_residual_cfo_hz: float
    full_search_control_score: float
    control_frames: StarlinkFrameScoreSummaryV0_2
    pilot_symbol_indices: tuple[int, ...]
    symbol_set_role: str
    symbol_split_digest: Digest | None
    control_search: str
    surrogate_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-full-search-control-method-evidence"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-search control method schema")
        if self.search_mode is StarlinkFullSearchControlMode.SEARCHED_ROLLED_TEMPLATE:
            if self.selection_method is not self.method:
                raise ValueError("searched control evidence must select itself")
        elif self.selection_method is not StarlinkDetectorMethod.FULL_FRAME_ACQUIRE:
            raise ValueError("conditioned control evidence must cite acquire")
        if (
            isinstance(self.effective_search_cell_count, bool)
            or not isinstance(self.effective_search_cell_count, int)
            or self.effective_search_cell_count <= 0
        ):
            raise ValueError("control search cell count must be positive")
        if (
            isinstance(self.winning_epoch_sample, bool)
            or not isinstance(self.winning_epoch_sample, int)
            or self.winning_epoch_sample < 0
        ):
            raise ValueError("control winning epoch must be non-negative")
        for name in (
            "winning_coarse_cfo_hz",
            "winning_residual_cfo_hz",
            "full_search_control_score",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.full_search_control_score <= 1:
            raise ValueError("full-search control score must lie in [0,1]")
        if self.control_frames.support <= 0:
            raise ValueError("full-search control requires frame support")
        if (
            not self.pilot_symbol_indices
            or tuple(sorted(set(self.pilot_symbol_indices)))
            != self.pilot_symbol_indices
            or self.pilot_symbol_indices[0] < 2
            or self.pilot_symbol_indices[-1] > 301
        ):
            raise ValueError("control pilot symbols must be a sorted subset of 2..301")
        if self.symbol_set_role not in (
            "anchor",
            "contiguous",
            "acquire",
            "verify",
            "full",
        ):
            raise ValueError("unknown control symbol-set role")
        if self.symbol_set_role in ("acquire", "verify", "full"):
            if self.symbol_split_digest is None:
                raise ValueError("full-frame control must cite its symbol split")
        elif self.symbol_split_digest is not None:
            raise ValueError("relative control cannot cite a full-frame split")
        if self.control_search != "rolled-template-independent-full-search":
            raise ValueError("control must use the independent rolled-template search")
        if not self.surrogate_only:
            raise ValueError("rolled-template control cannot be a detection verdict")
        if not {
            "same-hypothesis-grid-as-target",
            "surrogate-control-not-verified-signal-absent",
        } <= set(self.reason_codes):
            raise ValueError("full-search control must disclose its semantics")


@dataclass(frozen=True)
class StarlinkFullSearchControlSuiteV0_1:
    """All eight symmetric rolled-template scores for one receiver stream."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    probe_sample_count: int
    suite_identity_digest: Digest
    methods: tuple[StarlinkFullSearchControlMethodEvidenceV0_1, ...]
    provenance: Provenance
    surrogate_only: bool
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-full-search-control-suite"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-search control suite schema")
        if not self.analysis_id.startswith("slsctrl_"):
            raise ValueError("invalid full-search control analysis identity")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("full-search control input dimensions must be positive")
        if tuple(item.method for item in self.methods) != REPORT_METHOD_ORDER:
            raise ValueError("full-search control must contain every report method")
        if len({item.method for item in self.methods}) != len(self.methods):
            raise ValueError("full-search control contains duplicate methods")
        if not self.surrogate_only:
            raise ValueError("full-search control suite cannot emit a verdict")
        if "not-an-empirical-null-distribution" not in self.warnings:
            raise ValueError("full-search control suite must disclose its limitation")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


class StarlinkFullSearchControlRecordingState(str, Enum):
    CANDIDATES = "candidates"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class StarlinkFullSearchControlRecordingBundleV0_1:
    """Backfillable symmetric controls for one immutable recording request."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    source_request_digest: Digest
    state: StarlinkFullSearchControlRecordingState
    suites: tuple[StarlinkFullSearchControlSuiteV0_1, ...]
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-full-search-control-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported full-search control recording schema")
        if not self.analysis_id.startswith("slsctrlrec_"):
            raise ValueError("invalid full-search control recording identity")
        keys = tuple((item.segment_id, item.receiver_chain_id) for item in self.suites)
        if keys != tuple(
            sorted(keys, key=lambda value: (str(value[0]), str(value[1])))
        ):
            raise ValueError("full-search control streams must be canonical")
        if len(keys) != len(set(keys)) or len(keys) > 64:
            raise ValueError("full-search control streams are duplicate or unbounded")
        if any(
            item.recording_id != self.recording_id
            or item.recording_identity_digest != self.recording_identity_digest
            for item in self.suites
        ):
            raise ValueError("full-search control suite belongs to another recording")
        if self.state is StarlinkFullSearchControlRecordingState.CANDIDATES:
            if not self.suites or "surrogate-control-only" not in self.reason_codes:
                raise ValueError("full-search control candidates require disclosure")
        elif self.suites or self.reason_codes != ("clipped-pilot-band",):
            raise ValueError("not-evaluated full-search control must be empty")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
