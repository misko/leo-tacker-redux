"""Pure, deterministic, bounded post-capture waterfall analysis v0.1."""

from __future__ import annotations

import math
from dataclasses import dataclass

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.waterfall import (
    MAX_WATERFALL_CELLS,
    MAX_WATERFALL_FREQUENCY_BINS,
    MAX_WATERFALL_TILES,
    MAX_WATERFALL_TIME_BINS_PER_TILE,
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
    WaterfallProductId,
    WaterfallTileV0_1,
    WaterfallTimeBinV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import (
    AnalysisConfigurationError,
    AnalysisExecutionContext,
    AnalysisInputError,
)
from .psd import radix2_fft
from .quality import Ci16DecodeError, decode_ci16

WATERFALL_ALGORITHM_ID = "bounded-waterfall"
WATERFALL_ALGORITHM_VERSION = "0.1.0"
WATERFALL_CONFIG_SCHEMA_ID = "org.leo-flow.waterfall-config"


@dataclass(frozen=True)
class WaterfallConfigV0_1:
    """Scientific and resource choices hashed into every result identity."""

    fft_window_samples: int = 256
    frequency_bins: int = 128
    maximum_time_bins_per_tile: int = 128
    maximum_tiles: int = MAX_WATERFALL_TILES
    maximum_total_cells: int = MAX_WATERFALL_CELLS
    power_floor_counts_squared: float = 1e-12

    def __post_init__(self) -> None:
        if (
            isinstance(self.fft_window_samples, bool)
            or not isinstance(self.fft_window_samples, int)
            or self.fft_window_samples < 8
            or self.fft_window_samples > 4096
            or self.fft_window_samples & (self.fft_window_samples - 1)
        ):
            raise ValueError("fft_window_samples must be a power of two in [8, 4096]")
        if (
            isinstance(self.frequency_bins, bool)
            or not isinstance(self.frequency_bins, int)
            or self.frequency_bins <= 0
            or self.frequency_bins > MAX_WATERFALL_FREQUENCY_BINS
            or self.frequency_bins & (self.frequency_bins - 1)
            or self.frequency_bins > self.fft_window_samples
        ):
            raise ValueError(
                "frequency_bins must be a bounded power of two no larger than FFT"
            )
        for value, bound, name in (
            (
                self.maximum_time_bins_per_tile,
                MAX_WATERFALL_TIME_BINS_PER_TILE,
                "maximum_time_bins_per_tile",
            ),
            (self.maximum_tiles, MAX_WATERFALL_TILES, "maximum_tiles"),
            (
                self.maximum_total_cells,
                MAX_WATERFALL_CELLS,
                "maximum_total_cells",
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 < value <= bound
            ):
                raise ValueError(f"{name} exceeds its public contract bound")
        if (
            isinstance(self.power_floor_counts_squared, bool)
            or not isinstance(self.power_floor_counts_squared, (int, float))
            or not math.isfinite(self.power_floor_counts_squared)
            or self.power_floor_counts_squared <= 0
        ):
            raise ValueError("power_floor_counts_squared must be positive and finite")


def waterfall_algorithm_ref_v0_1() -> ArtifactRef:
    descriptor = {
        "algorithm_id": WATERFALL_ALGORITHM_ID,
        "algorithm_version": WATERFALL_ALGORITHM_VERSION,
        "input": "org.leo-flow.recording-view/v0.1",
        "output": f"{WaterfallBundleV0_1.SCHEMA_ID}/0.1",
        "window": "periodic-hann-demeaned-radix2-fft",
        "time_selection": "evenly-spaced-nonoverlapping-verified-rf-windows",
        "frequency_reduction": "mean-power-contiguous-fftshifted-groups",
    }
    return ArtifactRef(
        "bounded-waterfall-v0.1",
        canonical_digest(descriptor),
        SchemaRef("org.leo-flow.recording-waterfall-algorithm"),
    )


def waterfall_config_ref_v0_1(config: WaterfallConfigV0_1) -> ArtifactRef:
    return ArtifactRef(
        "bounded-waterfall-config-v0.1",
        canonical_digest(config),
        SchemaRef(WATERFALL_CONFIG_SCHEMA_ID),
    )


def _selected_indices(candidate_count: int, selected_count: int) -> tuple[int, ...]:
    if not 0 < selected_count <= candidate_count:
        raise ValueError("selected count must be within candidate count")
    if selected_count == 1:
        return (candidate_count // 2,)
    denominator = selected_count - 1
    return tuple(
        (index * (candidate_count - 1) + denominator // 2) // denominator
        for index in range(selected_count)
    )


def _selected_window_starts(
    recording: RecordingView,
    segment_id: SegmentId,
    segment_sample_count: int,
    window_samples: int,
    maximum_windows: int,
) -> tuple[int, ...]:
    try:
        spans = recording.contiguous_rf_spans(segment_id)
    except Exception as error:
        raise AnalysisInputError(
            f"recording continuity read failed for {segment_id}"
        ) from error
    previous_stop = 0
    span_windows: list[tuple[int, int]] = []
    for span in spans:
        start = span.start_sample
        stop = span.stop_sample
        if (
            isinstance(start, bool)
            or isinstance(stop, bool)
            or not isinstance(start, int)
            or not isinstance(stop, int)
            or start < previous_stop
            or not 0 <= start < stop <= segment_sample_count
        ):
            raise AnalysisInputError("recording returned invalid contiguous RF spans")
        count = (stop - start) // window_samples
        if count:
            span_windows.append((start, count))
        previous_stop = stop
    candidate_count = sum(count for _, count in span_windows)
    if candidate_count == 0:
        return ()
    selected_count = min(maximum_windows, candidate_count)
    global_indices = _selected_indices(candidate_count, selected_count)
    result: list[int] = []
    span_index = 0
    offset = 0
    for global_index in global_indices:
        while global_index >= offset + span_windows[span_index][1]:
            offset += span_windows[span_index][1]
            span_index += 1
        span_start, _ = span_windows[span_index]
        result.append(span_start + (global_index - offset) * window_samples)
    return tuple(result)


def _frequency_axis(
    fft_samples: int, frequency_bins: int, sample_rate_hz: float
) -> tuple[float, ...]:
    group_size = fft_samples // frequency_bins
    signed_bins = tuple(range(-fft_samples // 2, fft_samples // 2))
    return tuple(
        round(
            sum(signed_bins[start : start + group_size])
            * sample_rate_hz
            / (group_size * fft_samples),
            9,
        )
        for start in range(0, fft_samples, group_size)
    )


def _power_row_db(
    values: list[complex], config: WaterfallConfigV0_1
) -> tuple[float, ...]:
    count = len(values)
    mean = sum(values) / count
    window = tuple(
        0.5 - 0.5 * math.cos(2.0 * math.pi * index / count) for index in range(count)
    )
    spectrum = radix2_fft(
        [(sample - mean) * weight for sample, weight in zip(values, window)]
    )
    scale = sum(weight * weight for weight in window)
    powers = tuple(
        (value.real * value.real + value.imag * value.imag) / scale
        for value in spectrum[count // 2 :] + spectrum[: count // 2]
    )
    group_size = count // config.frequency_bins
    return tuple(
        round(
            10.0
            * math.log10(
                max(
                    sum(powers[start : start + group_size]) / group_size,
                    config.power_floor_counts_squared,
                )
            ),
            6,
        )
        for start in range(0, count, group_size)
    )


class BoundedWaterfallAnalyzerV0_1:
    """Generate one immutable display product from one completed recording."""

    def __init__(
        self, config: WaterfallConfigV0_1, execution: AnalysisExecutionContext
    ) -> None:
        self._config = config
        self._execution = execution

    @property
    def maximum_read_samples(self) -> int:
        return self._config.fft_window_samples

    def analyze_waterfall(
        self, recording: RecordingView, request: WaterfallAnalysisRequestV0_1
    ) -> WaterfallBundleV0_1:
        manifest = recording.manifest
        if manifest.recording_id != request.recording_id:
            raise AnalysisInputError("recording view and waterfall request IDs differ")
        if request.algorithm_ref != waterfall_algorithm_ref_v0_1():
            raise AnalysisConfigurationError(
                "waterfall request algorithm_ref does not identify this analyzer"
            )
        if request.config_ref != waterfall_config_ref_v0_1(self._config):
            raise AnalysisConfigurationError(
                "waterfall request config_ref does not match analyzer config"
            )
        if request.requested_output_schema != SchemaRef(WaterfallBundleV0_1.SCHEMA_ID):
            raise AnalysisConfigurationError(
                "waterfall request does not select bundle v0.1"
            )
        tile_count = sum(
            len(segment.requested.receiver_chain_ids) for segment in manifest.segments
        )
        if tile_count == 0:
            raise AnalysisInputError("recording contains no waterfall receivers")
        if tile_count > self._config.maximum_tiles:
            raise AnalysisInputError(
                "recording exceeds configured waterfall tile bound"
            )
        time_limit = min(
            self._config.maximum_time_bins_per_tile,
            self._config.maximum_total_cells
            // (tile_count * self._config.frequency_bins),
        )
        if time_limit <= 0:
            raise AnalysisConfigurationError(
                "waterfall cell bound cannot provide one row per tile"
            )

        dependencies = tuple(
            sorted(
                request.dependency_refs,
                key=lambda ref: (ref.artifact_id, str(ref.digest)),
            )
        )
        recording_digest = request.recording_object_ref.identity_digest()
        run_identity = {
            "recording_id": str(request.recording_id),
            "recording_identity_digest": str(recording_digest),
            "algorithm_digest": str(request.algorithm_ref.digest),
            "config_digest": str(request.config_ref.digest),
            "dependency_digests": [str(ref.digest) for ref in dependencies],
            "environment_digest": str(self._execution.environment_digest),
            "git_commit": self._execution.git_commit,
        }
        token = canonical_digest(run_identity).value
        tiles: list[WaterfallTileV0_1] = []
        warnings: list[str] = []
        reason_codes: set[str] = set()
        for segment in manifest.segments:
            starts = _selected_window_starts(
                recording,
                segment.segment_id,
                segment.sample_count,
                self._config.fft_window_samples,
                time_limit,
            )
            if not starts:
                warnings.append(f"{segment.segment_id}:segment-too-short")
                reason_codes.add("waterfall-skipped-short-segment")
                continue
            receiver_ids = segment.requested.receiver_chain_ids
            rows: list[list[WaterfallTimeBinV0_1]] = [[] for _ in receiver_ids]
            for start in starts:
                stop = start + self._config.fft_window_samples
                raw = self._read_exact(
                    recording,
                    segment.segment_id,
                    start,
                    stop,
                    len(receiver_ids),
                )
                try:
                    decoded, sample_count = decode_ci16(raw, len(receiver_ids))
                except Ci16DecodeError as error:
                    raise AnalysisInputError(
                        f"segment {segment.segment_id} waterfall CI16 is malformed"
                    ) from error
                if sample_count != self._config.fft_window_samples:
                    raise AnalysisInputError(
                        "waterfall window decoded to an unexpected sample count"
                    )
                stride = len(receiver_ids) * 2
                for receiver_index in range(len(receiver_ids)):
                    offset = receiver_index * 2
                    samples = [
                        complex(decoded[position], decoded[position + 1])
                        for position in range(offset, len(decoded), stride)
                    ]
                    midpoint = UtcNs(
                        segment.start_utc_ns
                        + round(
                            (start + stop) * 500_000_000 / segment.actual_sample_rate_hz
                        )
                    )
                    rows[receiver_index].append(
                        WaterfallTimeBinV0_1(
                            start,
                            stop,
                            midpoint,
                            _power_row_db(samples, self._config),
                        )
                    )
            frequency_axis = _frequency_axis(
                self._config.fft_window_samples,
                self._config.frequency_bins,
                segment.actual_sample_rate_hz,
            )
            for receiver_id, receiver_rows in zip(receiver_ids, rows, strict=True):
                tiles.append(
                    WaterfallTileV0_1(
                        segment.segment_id,
                        receiver_id,
                        segment.start_utc_ns,
                        segment.sample_count,
                        segment.actual_center_frequency_hz,
                        segment.actual_sample_rate_hz,
                        self._config.fft_window_samples,
                        "counts-squared-per-bin",
                        frequency_axis,
                        tuple(receiver_rows),
                    )
                )
        if not tiles:
            raise AnalysisInputError("recording has no complete waterfall window")
        if len(tiles) != tile_count:
            reason_codes.add("waterfall-segments-omitted")
        provenance = Provenance(
            producer_name=self._execution.producer_name,
            producer_version=self._execution.producer_version,
            git_commit=self._execution.git_commit,
            environment_digest=self._execution.environment_digest,
            normalized_config_digest=request.config_ref.digest,
            input_digests=(recording_digest,),
            dependency_digests=(request.algorithm_ref.digest,)
            + tuple(item.digest for item in dependencies),
            started_utc_ns=self._execution.started_utc_ns,
            completed_utc_ns=self._execution.completed_utc_ns,
            host_class=self._execution.host_class,
        )
        return WaterfallBundleV0_1(
            schema=SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
            product_id=WaterfallProductId(f"waterfall_{token[32:64]}"),
            analysis_run_id=AnalysisRunId(f"arun_{token[:32]}"),
            recording_id=request.recording_id,
            input_recording_identity_digest=recording_digest,
            provenance=provenance,
            tiles=tuple(tiles),
            warnings=tuple(sorted(set(warnings))),
            reason_codes=tuple(sorted(reason_codes)),
        )

    @staticmethod
    def _read_exact(
        recording: RecordingView,
        segment_id: SegmentId,
        start: int,
        stop: int,
        receiver_count: int,
    ) -> bytes:
        expected_bytes = (stop - start) * receiver_count * 4
        try:
            raw = recording.read_iq_bytes(segment_id, start, stop)
        except Exception as error:
            raise AnalysisInputError(
                f"recording reader failed for {segment_id}[{start}:{stop}]"
            ) from error
        if not isinstance(raw, bytes):
            raise AnalysisInputError("recording reader must return immutable bytes")
        if len(raw) != expected_bytes:
            raise AnalysisInputError(
                f"recording reader returned {len(raw)} bytes; expected {expected_bytes}"
            )
        return raw
