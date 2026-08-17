"""Additive, bounded full-coverage waterfall analysis contracts v0.2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from leo_flow.storage.ports import RecordingView

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from .storage import ObjectRef, RecordingObjectRef
from .waterfall import WaterfallProductId

V0_2 = SchemaVersion(0, 2)

MAX_WATERFALL_V0_2_TILES = 16
MAX_WATERFALL_V0_2_TIME_BINS_PER_TILE = 256
MAX_WATERFALL_V0_2_FREQUENCY_BINS = 512
MAX_WATERFALL_V0_2_PIXELS = 524_288
MAX_WATERFALL_V0_2_JSON_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True)
class WaterfallAnalysisRequestV0_2:
    """Select one recording and the independent v0.2 waterfall implementation."""

    schema: SchemaRef
    recording_id: RecordingId
    recording_object_ref: RecordingObjectRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    SCHEMA_ID = "org.leo-flow.waterfall-analysis-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported waterfall v0.2 analysis request")
        if self.recording_id != self.recording_object_ref.recording_id:
            raise ValueError("waterfall request recording IDs differ")
        if self.requested_output_schema != SchemaRef(
            WaterfallBundleV0_2.SCHEMA_ID, V0_2
        ):
            raise ValueError("unsupported waterfall v0.2 output schema")
        dependency_ids = tuple(item.artifact_id for item in self.dependency_refs)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("waterfall dependency refs contain duplicate IDs")


@dataclass(frozen=True)
class WaterfallCoverageV0_2:
    """Auditable accounting of RF samples eligible for complete FFT frames."""

    contiguous_rf_span_count: int
    contiguous_rf_sample_count: int
    analyzed_sample_count: int
    discarded_tail_sample_count: int
    fft_frame_count: int
    coverage_fraction: float

    def __post_init__(self) -> None:
        for name in (
            "contiguous_rf_span_count",
            "contiguous_rf_sample_count",
            "analyzed_sample_count",
            "discarded_tail_sample_count",
            "fft_frame_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.contiguous_rf_span_count <= 0 or self.contiguous_rf_sample_count <= 0:
            raise ValueError("coverage requires non-empty contiguous RF input")
        if (
            self.analyzed_sample_count + self.discarded_tail_sample_count
            != self.contiguous_rf_sample_count
        ):
            raise ValueError("waterfall coverage sample accounting is inconsistent")
        require_finite(self.coverage_fraction, "coverage_fraction")
        expected = self.analyzed_sample_count / self.contiguous_rf_sample_count
        if abs(self.coverage_fraction - expected) > 1e-12:
            raise ValueError("waterfall coverage fraction is inconsistent")


@dataclass(frozen=True)
class WaterfallTimeBinV0_2:
    """A bounded display row aggregating complete FFT frames in linear power."""

    start_sample: int
    stop_sample: int
    midpoint_utc_ns: UtcNs
    analyzed_sample_count: int
    fft_frame_count: int
    fft_frame_start_samples: tuple[int, ...]
    average_power_db: tuple[float, ...]
    temporal_median_residual_db: tuple[float, ...]
    high_percentile_power_db: tuple[float, ...]

    def __post_init__(self) -> None:
        for name in (
            "start_sample",
            "stop_sample",
            "analyzed_sample_count",
            "fft_frame_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not 0 <= self.start_sample < self.stop_sample:
            raise ValueError("waterfall time-bin interval is invalid")
        if self.analyzed_sample_count <= 0 or self.fft_frame_count <= 0:
            raise ValueError("waterfall time bin requires analyzed FFT frames")
        if len(self.fft_frame_start_samples) != self.fft_frame_count:
            raise ValueError("waterfall time bin must identify every FFT frame")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.fft_frame_start_samples
        ):
            raise ValueError("FFT frame starts must be non-negative integers")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.fft_frame_start_samples,
                self.fft_frame_start_samples[1:],
                strict=False,
            )
        ):
            raise ValueError("FFT frame starts must be strictly increasing")
        if self.analyzed_sample_count > self.stop_sample - self.start_sample:
            raise ValueError("analyzed samples cannot exceed the represented interval")
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        lengths = {
            len(self.average_power_db),
            len(self.temporal_median_residual_db),
            len(self.high_percentile_power_db),
        }
        if len(lengths) != 1 or not self.average_power_db:
            raise ValueError("waterfall v0.2 layer widths must be equal and non-empty")
        for layer in (
            self.average_power_db,
            self.temporal_median_residual_db,
            self.high_percentile_power_db,
        ):
            for value in layer:
                require_finite(value, "waterfall layer value")


@dataclass(frozen=True)
class WaterfallTileV0_2:
    """Three-layer spectrogram for one exact segment and receiver stream."""

    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    segment_start_utc_ns: UtcNs
    segment_sample_count: int
    center_frequency_hz: float
    sample_rate_hz: float
    fft_window_samples: int
    fft_hop_samples: int
    display_frequency_bins: int
    power_reference: str
    high_percentile: float
    frequency_bin_offsets_hz: tuple[float, ...]
    coverage: WaterfallCoverageV0_2
    time_bins: tuple[WaterfallTimeBinV0_2, ...]

    def __post_init__(self) -> None:
        require_utc_ns(self.segment_start_utc_ns, "segment_start_utc_ns")
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        require_token(self.power_reference, "power_reference")
        for name in (
            "segment_sample_count",
            "fft_window_samples",
            "fft_hop_samples",
            "display_frequency_bins",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            self.fft_window_samples < 8
            or self.fft_window_samples > self.segment_sample_count
            or self.fft_window_samples & (self.fft_window_samples - 1)
        ):
            raise ValueError("fft_window_samples must be a power of two within segment")
        if self.fft_hop_samples != self.fft_window_samples:
            raise ValueError("v0.2 requires non-overlapping full-coverage FFT frames")
        if (
            self.display_frequency_bins > MAX_WATERFALL_V0_2_FREQUENCY_BINS
            or self.display_frequency_bins > self.fft_window_samples
            or self.fft_window_samples % self.display_frequency_bins
        ):
            raise ValueError("display frequency bins must evenly divide the FFT")
        require_finite(self.high_percentile, "high_percentile")
        if not 50.0 <= self.high_percentile <= 100.0:
            raise ValueError("high_percentile must be in [50, 100]")
        if len(self.frequency_bin_offsets_hz) != self.display_frequency_bins:
            raise ValueError("waterfall frequency axis width is inconsistent")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.frequency_bin_offsets_hz,
                self.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        ):
            raise ValueError("waterfall frequency offsets must be strictly increasing")
        for offset in self.frequency_bin_offsets_hz:
            require_finite(offset, "frequency_bin_offset_hz")
            if not -self.sample_rate_hz / 2 <= offset < self.sample_rate_hz / 2:
                raise ValueError("waterfall frequency offset is outside Nyquist")
        if not 0 < len(self.time_bins) <= MAX_WATERFALL_V0_2_TIME_BINS_PER_TILE:
            raise ValueError("waterfall v0.2 time-bin count exceeds its bound")
        previous_stop = -1
        frame_count = 0
        analyzed_count = 0
        for row in self.time_bins:
            if (
                row.start_sample < previous_stop
                or row.stop_sample > self.segment_sample_count
            ):
                raise ValueError("waterfall v0.2 rows overlap or exceed the segment")
            if (
                row.analyzed_sample_count
                != row.fft_frame_count * self.fft_window_samples
            ):
                raise ValueError(
                    "waterfall row frame/sample accounting is inconsistent"
                )
            if (
                row.fft_frame_start_samples[0] != row.start_sample
                or row.fft_frame_start_samples[-1] + self.fft_window_samples
                != row.stop_sample
                or any(
                    later - earlier < self.fft_hop_samples
                    for earlier, later in zip(
                        row.fft_frame_start_samples,
                        row.fft_frame_start_samples[1:],
                        strict=False,
                    )
                )
            ):
                raise ValueError("waterfall row does not exactly locate its FFT frames")
            if len(row.average_power_db) != self.display_frequency_bins:
                raise ValueError("waterfall row width differs from frequency axis")
            expected_midpoint = self.segment_start_utc_ns + round(
                (row.start_sample + row.stop_sample) * 500_000_000 / self.sample_rate_hz
            )
            if row.midpoint_utc_ns != expected_midpoint:
                raise ValueError("waterfall time-bin midpoint is inconsistent")
            previous_stop = row.stop_sample
            frame_count += row.fft_frame_count
            analyzed_count += row.analyzed_sample_count
        if (
            frame_count != self.coverage.fft_frame_count
            or analyzed_count != self.coverage.analyzed_sample_count
        ):
            raise ValueError("tile rows contradict waterfall coverage facts")


@dataclass(frozen=True)
class WaterfallBundleV0_2:
    """Bounded durable analysis product; v0.1 remains unchanged and decodable."""

    schema: SchemaRef
    product_id: WaterfallProductId
    analysis_run_id: AnalysisRunId
    recording_id: RecordingId
    input_recording_identity_digest: Digest
    provenance: Provenance
    tiles: tuple[WaterfallTileV0_2, ...]
    warnings: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.waterfall-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_2):
            raise ValueError("unsupported waterfall bundle v0.2 schema")
        if not 0 < len(self.tiles) <= MAX_WATERFALL_V0_2_TILES:
            raise ValueError("waterfall v0.2 tile count exceeds its bound")
        keys = tuple((tile.segment_id, tile.receiver_chain_id) for tile in self.tiles)
        if keys != tuple(sorted(keys, key=lambda item: (str(item[0]), str(item[1])))):
            raise ValueError("waterfall v0.2 tiles must use canonical identity order")
        if len(keys) != len(set(keys)):
            raise ValueError("waterfall v0.2 tile identities must be unique")
        pixels = sum(
            len(tile.time_bins) * tile.display_frequency_bins for tile in self.tiles
        )
        if pixels > MAX_WATERFALL_V0_2_PIXELS:
            raise ValueError("waterfall v0.2 pixel count exceeds its bound")
        for collection, name in (
            (self.warnings, "warning"),
            (self.reason_codes, "reason_code"),
        ):
            if tuple(sorted(set(collection))) != collection:
                raise ValueError(f"waterfall {name}s must be unique and sorted")
            for value in collection:
                require_token(value, name)
        if len(canonical_json_bytes(self)) > MAX_WATERFALL_V0_2_JSON_BYTES:
            raise ValueError("waterfall v0.2 JSON exceeds its byte bound")


@dataclass(frozen=True)
class WaterfallProductRefV0_2:
    product_id: WaterfallProductId
    analysis_run_id: AnalysisRunId
    recording_id: RecordingId
    bundle_ref: ObjectRef


class WaterfallAnalyzerV0_2(Protocol):
    def analyze_waterfall(
        self, recording: RecordingView, request: WaterfallAnalysisRequestV0_2
    ) -> WaterfallBundleV0_2: ...
