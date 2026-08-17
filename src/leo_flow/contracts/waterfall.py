"""Versioned, bounded post-capture waterfall analysis contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    AnalysisRunId,
    ArtifactRef,
    ContractId,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from .storage import ObjectRef, RecordingObjectRef

MAX_WATERFALL_TILES = 64
MAX_WATERFALL_TIME_BINS_PER_TILE = 128
MAX_WATERFALL_FREQUENCY_BINS = 128
MAX_WATERFALL_CELLS = 262_144


class WaterfallProductId(ContractId):
    prefix = "waterfall"


@dataclass(frozen=True)
class WaterfallAnalysisRequestV0_1:
    """Select one exact recording and one exact waterfall implementation."""

    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.waterfall-analysis-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported waterfall analysis request")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("waterfall request recording IDs differ")
        if self.requested_output_schema != SchemaRef(WaterfallBundleV0_1.SCHEMA_ID):
            raise ValueError("unsupported waterfall analysis output schema")
        dependency_ids = tuple(item.artifact_id for item in self.dependency_refs)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("waterfall dependency refs contain duplicate IDs")


@dataclass(frozen=True)
class WaterfallTimeBinV0_1:
    """One FFT window and its frequency-ordered power row."""

    start_sample: int
    stop_sample: int
    midpoint_utc_ns: UtcNs
    power_db: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_sample, bool)
            or not isinstance(self.start_sample, int)
            or isinstance(self.stop_sample, bool)
            or not isinstance(self.stop_sample, int)
            or not 0 <= self.start_sample < self.stop_sample
        ):
            raise ValueError("waterfall time bin sample interval is invalid")
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        if not self.power_db:
            raise ValueError("waterfall time bin requires frequency power")
        for value in self.power_db:
            require_finite(value, "power_db")


@dataclass(frozen=True)
class WaterfallTileV0_1:
    """Bounded spectrogram for one exact recording segment and receiver."""

    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    segment_start_utc_ns: UtcNs
    segment_sample_count: int
    center_frequency_hz: float
    sample_rate_hz: float
    fft_window_samples: int
    power_reference: str
    frequency_bin_offsets_hz: tuple[float, ...]
    time_bins: tuple[WaterfallTimeBinV0_1, ...]

    def __post_init__(self) -> None:
        require_utc_ns(self.segment_start_utc_ns, "segment_start_utc_ns")
        if (
            isinstance(self.segment_sample_count, bool)
            or not isinstance(self.segment_sample_count, int)
            or self.segment_sample_count <= 0
        ):
            raise ValueError("segment_sample_count must be a positive integer")
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if (
            isinstance(self.fft_window_samples, bool)
            or not isinstance(self.fft_window_samples, int)
            or self.fft_window_samples < 8
            or self.fft_window_samples & (self.fft_window_samples - 1)
        ):
            raise ValueError("fft_window_samples must be a power of two >= 8")
        require_token(self.power_reference, "power_reference")
        frequency_count = len(self.frequency_bin_offsets_hz)
        if not 0 < frequency_count <= MAX_WATERFALL_FREQUENCY_BINS:
            raise ValueError("waterfall frequency bin count exceeds its bound")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.frequency_bin_offsets_hz,
                self.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        ):
            raise ValueError("waterfall frequency offsets must be strictly increasing")
        for value in self.frequency_bin_offsets_hz:
            require_finite(value, "frequency_bin_offset_hz")
            if not -self.sample_rate_hz / 2 <= value < self.sample_rate_hz / 2:
                raise ValueError("waterfall frequency offset is outside Nyquist")
        if not 0 < len(self.time_bins) <= MAX_WATERFALL_TIME_BINS_PER_TILE:
            raise ValueError("waterfall time bin count exceeds its bound")
        previous_stop = -1
        for row in self.time_bins:
            if row.start_sample < previous_stop:
                raise ValueError("waterfall time bins overlap or are out of order")
            if (
                row.stop_sample > self.segment_sample_count
                or row.stop_sample - row.start_sample != self.fft_window_samples
            ):
                raise ValueError("waterfall time bin lies outside its segment")
            if len(row.power_db) != frequency_count:
                raise ValueError("waterfall power row and frequency axis differ")
            expected_midpoint = self.segment_start_utc_ns + round(
                (row.start_sample + row.stop_sample) * 500_000_000 / self.sample_rate_hz
            )
            if row.midpoint_utc_ns != expected_midpoint:
                raise ValueError("waterfall time bin midpoint is inconsistent")
            previous_stop = row.stop_sample


@dataclass(frozen=True)
class WaterfallBundleV0_1:
    """Immutable post-capture output; never part of capture completion."""

    schema: SchemaRef
    product_id: WaterfallProductId
    analysis_run_id: AnalysisRunId
    recording_id: RecordingId
    input_recording_identity_digest: Digest
    provenance: Provenance
    tiles: tuple[WaterfallTileV0_1, ...]
    warnings: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.waterfall-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported waterfall bundle schema")
        if not 0 < len(self.tiles) <= MAX_WATERFALL_TILES:
            raise ValueError("waterfall tile count exceeds its bound")
        keys = tuple((tile.segment_id, tile.receiver_chain_id) for tile in self.tiles)
        if len(keys) != len(set(keys)):
            raise ValueError("waterfall tile identities must be unique")
        cells = sum(
            len(tile.time_bins) * len(tile.frequency_bin_offsets_hz)
            for tile in self.tiles
        )
        if cells > MAX_WATERFALL_CELLS:
            raise ValueError("waterfall cell count exceeds its bound")


@dataclass(frozen=True)
class WaterfallProductRefV0_1:
    product_id: WaterfallProductId
    analysis_run_id: AnalysisRunId
    recording_id: RecordingId
    bundle_ref: ObjectRef


class WaterfallAnalyzerV0_1(Protocol):
    def analyze_waterfall(
        self, recording: RecordingView, request: WaterfallAnalysisRequestV0_1
    ) -> WaterfallBundleV0_1: ...
