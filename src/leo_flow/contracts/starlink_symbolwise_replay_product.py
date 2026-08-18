"""Recording-level durable product for bounded legacy symbolwise replay."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ._validation import require_finite, require_token
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
from .starlink_symbolwise_replay import (
    MAXIMUM_REPLAY_PATTERNS,
    MAXIMUM_REPLAY_WINDOWS,
    StarlinkReceiverFrequencyCenterV0_1,
    StarlinkSymbolwiseReplayBundleV0_1,
    StarlinkSymbolwiseWindowEvidenceV0_1,
)
from .storage import ObjectRef, RecordingObjectRef

V0_1 = SchemaVersion(0, 1)
MAXIMUM_RECORDING_REPLAY_STREAMS = 16
MAXIMUM_RECORDING_REPLAY_WINDOWS = (
    MAXIMUM_RECORDING_REPLAY_STREAMS * MAXIMUM_REPLAY_WINDOWS
)
MAXIMUM_RECORDING_REPLAY_QUERY_WINDOWS = 4096


@dataclass(frozen=True)
class StarlinkSymbolwiseRecordingPlanV0_1:
    """Frozen parity science and hard per-stream resource ceilings."""

    dwell_duration_s: float = 60.0
    window_duration_s: float = 0.010
    cadence_s: float = 0.100
    surrogate_count: int = 4
    maximum_windows: int = 600
    maximum_window_samples: int = 50_000
    maximum_timing_search_cells: int = 100_000_000
    maximum_refinement_search_cells: int = 1_000_000
    maximum_working_bytes: int = 64 * 1024 * 1024
    admission_mode: str = "explicit-on-demand-or-backfill"

    def __post_init__(self) -> None:
        for name, expected in (
            ("dwell_duration_s", 60.0),
            ("window_duration_s", 0.010),
            ("cadence_s", 0.100),
        ):
            require_finite(getattr(self, name), name)
            if getattr(self, name) != expected:
                raise ValueError(f"v0.1 {name} is fixed at {expected}")
        if self.surrogate_count != MAXIMUM_REPLAY_PATTERNS - 1:
            raise ValueError("recording replay requires Qin plus four surrogates")
        if self.maximum_windows != MAXIMUM_REPLAY_WINDOWS:
            raise ValueError("recording replay requires exactly 600 planned windows")
        for name in (
            "maximum_window_samples",
            "maximum_timing_search_cells",
            "maximum_refinement_search_cells",
            "maximum_working_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.admission_mode != "explicit-on-demand-or-backfill":
            raise ValueError("symbolwise replay cannot be default capture work")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayStreamSelectionV0_1:
    radio_id: RadioId
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    segment_sample_count: int
    frequency_center: StarlinkReceiverFrequencyCenterV0_1

    def __post_init__(self) -> None:
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz <= 0:
            raise ValueError("symbolwise stream sample rate must be positive")
        if self.segment_sample_count != round(self.sample_rate_hz * 60.0):
            raise ValueError("v0.1 recording replay requires an exact 60 second dwell")
        window_samples = round(self.sample_rate_hz * 0.010)
        cadence_samples = round(self.sample_rate_hz * 0.100)
        starts = tuple(
            range(0, self.segment_sample_count - window_samples + 1, cadence_samples)
        )
        if len(starts) != MAXIMUM_REPLAY_WINDOWS:
            raise ValueError("symbolwise stream does not produce 600 replay windows")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            str(self.radio_id),
            str(self.segment_id),
            str(self.receiver_chain_id),
            self.edge.value,
        )


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayRequestV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    plan: StarlinkSymbolwiseRecordingPlanV0_1
    stream_selections: tuple[StarlinkSymbolwiseReplayStreamSelectionV0_1, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.starlink-symbolwise-replay-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported symbolwise replay request schema")
        if self.requested_output_schema != SchemaRef(
            StarlinkSymbolwiseRecordingBundleV0_1.SCHEMA_ID, V0_1
        ):
            raise ValueError("unsupported symbolwise replay output schema")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("symbolwise replay recording identities differ")
        identities = tuple(item.identity for item in self.stream_selections)
        if (
            not identities
            or len(identities) > MAXIMUM_RECORDING_REPLAY_STREAMS
            or len(set(identities)) != len(identities)
            or identities != tuple(sorted(identities))
        ):
            raise ValueError("symbolwise replay streams must be unique and canonical")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSymbolwiseRecordingBundleV0_1:
    """Complete immutable per-window traces for one recording."""

    schema: SchemaRef
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    plan: StarlinkSymbolwiseRecordingPlanV0_1
    stream_selections: tuple[StarlinkSymbolwiseReplayStreamSelectionV0_1, ...]
    streams: tuple[StarlinkSymbolwiseReplayBundleV0_1, ...]
    provenance: Provenance
    candidates_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-symbolwise-recording-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording symbolwise replay schema")
        if not self.analysis_id.startswith("slsymrec_"):
            raise ValueError("invalid recording symbolwise replay identity")
        if len(self.streams) != len(self.stream_selections):
            raise ValueError("symbolwise replay stream membership differs")
        for selection, stream in zip(self.stream_selections, self.streams, strict=True):
            if (
                stream.recording_id != self.recording_id
                or stream.recording_identity_digest != self.recording_identity_digest
                or stream.segment_id != selection.segment_id
                or stream.receiver_chain_id != selection.receiver_chain_id
                or stream.edge is not selection.edge
                or stream.sample_rate_hz != selection.sample_rate_hz
                or stream.segment_sample_count != selection.segment_sample_count
                or stream.frequency_center != selection.frequency_center
            ):
                raise ValueError("symbolwise replay stream differs from its selection")
            if (
                len(stream.windows) != MAXIMUM_REPLAY_WINDOWS
                or len(stream.windows[0].patterns) != MAXIMUM_REPLAY_PATTERNS
                or stream.analyzed_union_sample_count
                != round(selection.sample_rate_hz * 6.0)
                or not math.isclose(stream.coverage_fraction, 0.1, abs_tol=1e-15)
            ):
                raise ValueError("symbolwise replay lost 600-window/10% accounting")
        if self.provenance.normalized_config_digest != self.plan.digest:
            raise ValueError("recording replay provenance uses another plan")
        dependencies = {stream.digest for stream in self.streams}
        if not dependencies <= set(self.provenance.dependency_digests):
            raise ValueError("recording replay provenance omits stream evidence")
        if not self.candidates_only:
            raise ValueError("symbolwise recording replay cannot emit a detection")
        required = {
            "finite-pattern-controls-not-empirical-null",
            "whole-search-calibration-required",
            "explicit-on-demand-or-backfill-only",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("recording replay omits required limitations")

    @property
    def total_window_count(self) -> int:
        return sum(len(stream.windows) for stream in self.streams)

    @property
    def total_pattern_evidence_count(self) -> int:
        return sum(
            len(window.patterns) for stream in self.streams for window in stream.windows
        )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayCatalogProjectionV0_1:
    analysis_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    request_digest: Digest
    stream_count: int
    window_count: int
    pattern_evidence_count: int
    candidates_only: bool

    def __post_init__(self) -> None:
        if not self.analysis_id.startswith("slsymrec_"):
            raise ValueError("invalid recording symbolwise projection identity")
        if not 1 <= self.stream_count <= MAXIMUM_RECORDING_REPLAY_STREAMS:
            raise ValueError("recording symbolwise stream count is out of bounds")
        if self.window_count != self.stream_count * MAXIMUM_REPLAY_WINDOWS:
            raise ValueError("recording symbolwise projection lost fixed cadence")
        if self.pattern_evidence_count != self.window_count * MAXIMUM_REPLAY_PATTERNS:
            raise ValueError("recording symbolwise projection lost pattern traces")
        if not self.candidates_only:
            raise ValueError("recording symbolwise projection cannot claim detection")


@dataclass(frozen=True)
class StarlinkSymbolwiseRecordingProductRefV0_1:
    analysis_id: str
    recording_id: RecordingId
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        require_token(self.analysis_id, "analysis_id")


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayPublicationFenceV0_1:
    """Exact durable-work lease authorized to publish one product."""

    work_id: str
    lease_token: str
    lease_generation: int

    def __post_init__(self) -> None:
        require_token(self.work_id, "work_id")
        require_token(self.lease_token, "lease_token")
        if (
            isinstance(self.lease_generation, bool)
            or not isinstance(self.lease_generation, int)
            or self.lease_generation <= 0
        ):
            raise ValueError("lease_generation must be a positive integer")


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayQueryV0_1:
    recording_id: RecordingId
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    first_window_index: int = 0
    stop_window_index: int = MAXIMUM_REPLAY_WINDOWS
    maximum_windows: int = MAXIMUM_RECORDING_REPLAY_QUERY_WINDOWS

    def __post_init__(self) -> None:
        if self.receiver_chain_ids != tuple(sorted(set(self.receiver_chain_ids))):
            raise ValueError("symbolwise query receivers must be canonical")
        if not 0 <= self.first_window_index < self.stop_window_index <= 600:
            raise ValueError("symbolwise query window interval is invalid")
        if not 1 <= self.maximum_windows <= MAXIMUM_RECORDING_REPLAY_QUERY_WINDOWS:
            raise ValueError("symbolwise query window bound is invalid")


@dataclass(frozen=True)
class StarlinkSymbolwiseReplayPresentationStreamV0_1:
    selection: StarlinkSymbolwiseReplayStreamSelectionV0_1
    total_window_count: int
    windows: tuple[StarlinkSymbolwiseWindowEvidenceV0_1, ...]


@dataclass(frozen=True)
class RecordingStarlinkSymbolwiseReplayViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    source_ref: ArtifactRef
    streams: tuple[StarlinkSymbolwiseReplayPresentationStreamV0_1, ...]
    original_window_count: int
    shown_window_count: int
    truncated: bool
    selection_rule: str
    candidates_only: bool
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.recording-starlink-symbolwise-replay-view"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording symbolwise replay view")
        shown = sum(len(stream.windows) for stream in self.streams)
        if shown != self.shown_window_count or shown > self.original_window_count:
            raise ValueError("symbolwise replay view counts are inconsistent")
        if self.truncated != (shown < self.original_window_count):
            raise ValueError("symbolwise replay truncation state is inconsistent")
        if self.selection_rule not in ("complete", "even-index-preserving"):
            raise ValueError("unsupported symbolwise replay view selection")
        if not self.candidates_only:
            raise ValueError("symbolwise replay view cannot claim detection")
