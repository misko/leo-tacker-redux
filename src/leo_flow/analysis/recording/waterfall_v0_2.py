"""Streaming, near-full-coverage waterfall analysis v0.2."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import require_token
from leo_flow.contracts.continuity import ContiguousRfSpan
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.waterfall import WaterfallProductId
from leo_flow.contracts.waterfall_v0_2 import (
    MAX_WATERFALL_V0_2_FREQUENCY_BINS,
    MAX_WATERFALL_V0_2_PIXELS,
    MAX_WATERFALL_V0_2_TILES,
    MAX_WATERFALL_V0_2_TIME_BINS_PER_TILE,
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
    WaterfallCoverageV0_2,
    WaterfallTileV0_2,
    WaterfallTimeBinV0_2,
)
from leo_flow.storage.ports import RecordingView

from .api import (
    AnalysisConfigurationError,
    AnalysisExecutionContext,
    AnalysisInputError,
)
from .psd import radix2_fft
from .quality import Ci16DecodeError, decode_ci16

WATERFALL_V0_2_ALGORITHM_ID = "full-coverage-waterfall"
WATERFALL_V0_2_ALGORITHM_VERSION = "0.2.0"
WATERFALL_V0_2_CONFIG_SCHEMA_ID = "org.leo-flow.waterfall-config"


class SpectrumPowerBackendV0_2(Protocol):
    """Narrow numerical port returning linear display-bin power for one frame."""

    @property
    def backend_id(self) -> str: ...

    def frame_power(
        self,
        raw_ci16: bytes,
        *,
        receiver_count: int,
        fft_window_samples: int,
        display_frequency_bins: int,
    ) -> tuple[tuple[float, ...], ...]: ...


@dataclass(frozen=True)
class WaterfallConfigV0_2:
    """Hashable scientific and resource choices for the v0.2 product."""

    fft_window_samples: int = 32_768
    display_frequency_bins: int = 512
    target_time_bins_per_tile: int = 200
    high_percentile: float = 95.0
    numerical_backend_id: str = "numpy-fft-v0.2"
    maximum_tiles: int = MAX_WATERFALL_V0_2_TILES
    maximum_total_pixels: int = 204_800
    power_floor_counts_squared: float = 1e-12

    def __post_init__(self) -> None:
        require_token(self.numerical_backend_id, "numerical_backend_id")
        if (
            isinstance(self.fft_window_samples, bool)
            or not isinstance(self.fft_window_samples, int)
            or self.fft_window_samples < 8
            or self.fft_window_samples > 131_072
            or self.fft_window_samples & (self.fft_window_samples - 1)
        ):
            raise ValueError("fft_window_samples must be a power of two in [8, 131072]")
        if (
            isinstance(self.display_frequency_bins, bool)
            or not isinstance(self.display_frequency_bins, int)
            or self.display_frequency_bins <= 0
            or self.display_frequency_bins > MAX_WATERFALL_V0_2_FREQUENCY_BINS
            or self.display_frequency_bins > self.fft_window_samples
            or self.fft_window_samples % self.display_frequency_bins
        ):
            raise ValueError("display_frequency_bins must evenly divide the FFT")
        if (
            isinstance(self.target_time_bins_per_tile, bool)
            or not isinstance(self.target_time_bins_per_tile, int)
            or not 0
            < self.target_time_bins_per_tile
            <= MAX_WATERFALL_V0_2_TIME_BINS_PER_TILE
        ):
            raise ValueError("target_time_bins_per_tile exceeds its public bound")
        if (
            isinstance(self.maximum_tiles, bool)
            or not isinstance(self.maximum_tiles, int)
            or not 0 < self.maximum_tiles <= MAX_WATERFALL_V0_2_TILES
        ):
            raise ValueError("maximum_tiles exceeds its public bound")
        if (
            isinstance(self.maximum_total_pixels, bool)
            or not isinstance(self.maximum_total_pixels, int)
            or not 0 < self.maximum_total_pixels <= MAX_WATERFALL_V0_2_PIXELS
        ):
            raise ValueError("maximum_total_pixels exceeds its public bound")
        for value, name in (
            (self.high_percentile, "high_percentile"),
            (self.power_floor_counts_squared, "power_floor_counts_squared"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite")
        if not 50.0 <= self.high_percentile <= 100.0:
            raise ValueError("high_percentile must be in [50, 100]")
        if self.power_floor_counts_squared <= 0:
            raise ValueError("power_floor_counts_squared must be positive")


def waterfall_algorithm_ref_v0_2() -> ArtifactRef:
    descriptor = {
        "algorithm_id": WATERFALL_V0_2_ALGORITHM_ID,
        "algorithm_version": WATERFALL_V0_2_ALGORITHM_VERSION,
        "input": "org.leo-flow.recording-view/v0.1",
        "output": f"{WaterfallBundleV0_2.SCHEMA_ID}/0.2",
        "window": "periodic-hann-demeaned-fft",
        "time_coverage": "all-complete-nonoverlapping-frames-in-contiguous-rf-spans",
        "time_reduction": "linear-power-mean-and-nearest-rank-high-percentile",
        "frequency_reduction": "linear-power-mean-of-contiguous-fftshifted-bins",
        "residual": "average-power-db-minus-per-frequency-temporal-median-db",
    }
    return ArtifactRef(
        "full-coverage-waterfall-v0.2",
        canonical_digest(descriptor),
        SchemaRef("org.leo-flow.recording-waterfall-algorithm", V0_2),
    )


def waterfall_config_ref_v0_2(config: WaterfallConfigV0_2) -> ArtifactRef:
    return ArtifactRef(
        "full-coverage-waterfall-config-v0.2",
        canonical_digest(config),
        SchemaRef(WATERFALL_V0_2_CONFIG_SCHEMA_ID, V0_2),
    )


class Radix2SpectrumPowerBackendV0_2:
    """Dependency-free numerical oracle suitable for small fixtures."""

    backend_id = "stdlib-radix2-v0.2"

    def frame_power(
        self,
        raw_ci16: bytes,
        *,
        receiver_count: int,
        fft_window_samples: int,
        display_frequency_bins: int,
    ) -> tuple[tuple[float, ...], ...]:
        try:
            decoded, sample_count = decode_ci16(raw_ci16, receiver_count)
        except Ci16DecodeError as error:
            raise ValueError("malformed CI16 FFT frame") from error
        if sample_count != fft_window_samples:
            raise ValueError("CI16 frame has an unexpected sample count")
        weights = tuple(
            0.5 - 0.5 * math.cos(2.0 * math.pi * index / fft_window_samples)
            for index in range(fft_window_samples)
        )
        scale = sum(weight * weight for weight in weights)
        group_size = fft_window_samples // display_frequency_bins
        rows: list[tuple[float, ...]] = []
        stride = receiver_count * 2
        for receiver_index in range(receiver_count):
            offset = receiver_index * 2
            samples = [
                complex(decoded[position], decoded[position + 1])
                for position in range(offset, len(decoded), stride)
            ]
            mean = sum(samples) / fft_window_samples
            spectrum = radix2_fft(
                [
                    (sample - mean) * weight
                    for sample, weight in zip(samples, weights, strict=True)
                ]
            )
            shifted = (
                spectrum[fft_window_samples // 2 :]
                + spectrum[: fft_window_samples // 2]
            )
            power = [
                (value.real * value.real + value.imag * value.imag) / scale
                for value in shifted
            ]
            rows.append(
                tuple(
                    sum(power[start : start + group_size]) / group_size
                    for start in range(0, fft_window_samples, group_size)
                )
            )
        return tuple(rows)


class NumpySpectrumPowerBackendV0_2:
    """Vectorized production backend supplied by the existing ``format`` extra."""

    backend_id = "numpy-fft-v0.2"

    def __init__(self) -> None:
        try:
            import numpy as np
        except ImportError as error:  # pragma: no cover - environment dependent
            raise AnalysisConfigurationError(
                "NumPy waterfall backend requires the project format extra"
            ) from error
        self._np = np

    def frame_power(
        self,
        raw_ci16: bytes,
        *,
        receiver_count: int,
        fft_window_samples: int,
        display_frequency_bins: int,
    ) -> tuple[tuple[float, ...], ...]:
        np = self._np
        expected_values = fft_window_samples * receiver_count * 2
        values = np.frombuffer(raw_ci16, dtype="<i2")
        if values.size != expected_values:
            raise ValueError("CI16 frame has an unexpected sample count")
        paired = values.reshape(fft_window_samples, receiver_count, 2)
        samples = paired[:, :, 0].astype(np.float64) + 1j * paired[:, :, 1]
        samples -= samples.mean(axis=0, keepdims=True)
        weights = np.hanning(fft_window_samples + 1)[:-1]
        spectrum = np.fft.fftshift(
            np.fft.fft(samples * weights[:, None], axis=0), axes=0
        )
        power = (spectrum.real**2 + spectrum.imag**2) / np.sum(weights**2)
        grouped = power.reshape(
            display_frequency_bins,
            fft_window_samples // display_frequency_bins,
            receiver_count,
        ).mean(axis=1)
        return tuple(
            tuple(float(value) for value in grouped[:, receiver_index])
            for receiver_index in range(receiver_count)
        )


def _configured_backend(backend_id: str) -> SpectrumPowerBackendV0_2:
    if backend_id == "numpy-fft-v0.2":
        return NumpySpectrumPowerBackendV0_2()
    if backend_id == "stdlib-radix2-v0.2":
        return Radix2SpectrumPowerBackendV0_2()
    raise AnalysisConfigurationError(
        "test-only waterfall numerical backend must be injected explicitly"
    )


def _frequency_axis(
    fft_samples: int, frequency_bins: int, sample_rate_hz: float
) -> tuple[float, ...]:
    group_size = fft_samples // frequency_bins
    return tuple(
        round(
            ((start + (group_size - 1) / 2) - fft_samples / 2)
            * sample_rate_hz
            / fft_samples,
            9,
        )
        for start in range(0, fft_samples, group_size)
    )


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered) / 100.0))
    return ordered[rank - 1]


def _db(value: float, floor: float) -> float:
    return round(10.0 * math.log10(max(value, floor)), 6)


@dataclass(frozen=True)
class _PreparedRow:
    start_sample: int
    stop_sample: int
    midpoint_utc_ns: UtcNs
    frame_starts: tuple[int, ...]
    frame_count: int
    receiver_average_db: tuple[tuple[float, ...], ...]
    receiver_percentile_db: tuple[tuple[float, ...], ...]


class FullCoverageWaterfallAnalyzerV0_2:
    """Aggregate every complete frame while bounding display dimensions and reads."""

    def __init__(
        self,
        config: WaterfallConfigV0_2,
        execution: AnalysisExecutionContext,
        backend: SpectrumPowerBackendV0_2 | None = None,
    ) -> None:
        self._config = config
        self._execution = execution
        self._backend = backend or _configured_backend(config.numerical_backend_id)
        if self._backend.backend_id != config.numerical_backend_id:
            raise AnalysisConfigurationError(
                "waterfall numerical backend does not match the hashed configuration"
            )

    @property
    def maximum_read_samples(self) -> int:
        return self._config.fft_window_samples

    @property
    def numerical_backend_id(self) -> str:
        return self._backend.backend_id

    def analyze_waterfall(
        self, recording: RecordingView, request: WaterfallAnalysisRequestV0_2
    ) -> WaterfallBundleV0_2:
        manifest = recording.manifest
        if manifest.recording_id != request.recording_id:
            raise AnalysisInputError("recording view and waterfall request IDs differ")
        if request.algorithm_ref != waterfall_algorithm_ref_v0_2():
            raise AnalysisConfigurationError(
                "waterfall request algorithm_ref does not identify v0.2"
            )
        if request.config_ref != waterfall_config_ref_v0_2(self._config):
            raise AnalysisConfigurationError(
                "waterfall request config_ref does not match v0.2 analyzer config"
            )
        if request.requested_output_schema != SchemaRef(
            WaterfallBundleV0_2.SCHEMA_ID, V0_2
        ):
            raise AnalysisConfigurationError("waterfall request does not select v0.2")

        tile_count = sum(
            len(segment.requested.receiver_chain_ids) for segment in manifest.segments
        )
        if tile_count == 0:
            raise AnalysisInputError("recording contains no waterfall receivers")
        if tile_count > self._config.maximum_tiles:
            raise AnalysisInputError(
                "recording exceeds configured waterfall tile bound"
            )
        row_limit = min(
            self._config.target_time_bins_per_tile,
            self._config.maximum_total_pixels
            // (tile_count * self._config.display_frequency_bins),
        )
        if row_limit <= 0:
            raise AnalysisConfigurationError(
                "waterfall pixel bound cannot provide one row per tile"
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
        tiles: list[WaterfallTileV0_2] = []
        warnings: list[str] = []
        reason_codes: set[str] = set()
        for segment in manifest.segments:
            spans = self._validated_spans(
                recording, segment.segment_id, segment.sample_count
            )
            frame_starts = tuple(
                start
                for span in spans
                for start in range(
                    span.start_sample,
                    span.stop_sample - self._config.fft_window_samples + 1,
                    self._config.fft_window_samples,
                )
            )
            contiguous_count = sum(
                span.stop_sample - span.start_sample for span in spans
            )
            analyzed_count = len(frame_starts) * self._config.fft_window_samples
            if not frame_starts:
                warnings.append(f"{segment.segment_id}:segment-too-short-for-v0.2-fft")
                reason_codes.add("waterfall-v0.2-skipped-short-segment")
                continue
            coverage = WaterfallCoverageV0_2(
                contiguous_rf_span_count=len(spans),
                contiguous_rf_sample_count=contiguous_count,
                analyzed_sample_count=analyzed_count,
                discarded_tail_sample_count=contiguous_count - analyzed_count,
                fft_frame_count=len(frame_starts),
                coverage_fraction=analyzed_count / contiguous_count,
            )
            if coverage.coverage_fraction < 0.95:
                warnings.append(f"{segment.segment_id}:coverage-below-95-percent")
                reason_codes.add("waterfall-v0.2-low-coverage")
            prepared = self._prepare_rows(
                recording,
                segment.segment_id,
                segment.start_utc_ns,
                segment.actual_sample_rate_hz,
                len(segment.requested.receiver_chain_ids),
                frame_starts,
                min(row_limit, len(frame_starts)),
            )
            frequency_axis = _frequency_axis(
                self._config.fft_window_samples,
                self._config.display_frequency_bins,
                segment.actual_sample_rate_hz,
            )
            receiver_ids = segment.requested.receiver_chain_ids
            for receiver_index, receiver_id in enumerate(receiver_ids):
                medians = tuple(
                    statistics.median(
                        row.receiver_average_db[receiver_index][frequency_index]
                        for row in prepared
                    )
                    for frequency_index in range(self._config.display_frequency_bins)
                )
                rows = tuple(
                    WaterfallTimeBinV0_2(
                        start_sample=row.start_sample,
                        stop_sample=row.stop_sample,
                        midpoint_utc_ns=row.midpoint_utc_ns,
                        analyzed_sample_count=(
                            row.frame_count * self._config.fft_window_samples
                        ),
                        fft_frame_count=row.frame_count,
                        fft_frame_start_samples=row.frame_starts,
                        average_power_db=row.receiver_average_db[receiver_index],
                        temporal_median_residual_db=tuple(
                            round(value - median, 6)
                            for value, median in zip(
                                row.receiver_average_db[receiver_index],
                                medians,
                                strict=True,
                            )
                        ),
                        high_percentile_power_db=(
                            row.receiver_percentile_db[receiver_index]
                        ),
                    )
                    for row in prepared
                )
                tiles.append(
                    WaterfallTileV0_2(
                        segment_id=segment.segment_id,
                        receiver_chain_id=receiver_id,
                        segment_start_utc_ns=segment.start_utc_ns,
                        segment_sample_count=segment.sample_count,
                        center_frequency_hz=segment.actual_center_frequency_hz,
                        sample_rate_hz=segment.actual_sample_rate_hz,
                        fft_window_samples=self._config.fft_window_samples,
                        fft_hop_samples=self._config.fft_window_samples,
                        display_frequency_bins=self._config.display_frequency_bins,
                        power_reference=(
                            "uncalibrated-counts-squared-per-native-fft-bin"
                        ),
                        high_percentile=self._config.high_percentile,
                        frequency_bin_offsets_hz=frequency_axis,
                        coverage=coverage,
                        time_bins=rows,
                    )
                )
        if not tiles:
            raise AnalysisInputError("recording has no complete waterfall v0.2 frame")
        if len(tiles) != tile_count:
            reason_codes.add("waterfall-v0.2-segments-omitted")
        tiles.sort(key=lambda tile: (str(tile.segment_id), str(tile.receiver_chain_id)))
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
        return WaterfallBundleV0_2(
            schema=SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
            product_id=WaterfallProductId(f"waterfall_{token[32:64]}"),
            analysis_run_id=AnalysisRunId(f"arun_{token[:32]}"),
            recording_id=request.recording_id,
            input_recording_identity_digest=recording_digest,
            provenance=provenance,
            tiles=tuple(tiles),
            warnings=tuple(sorted(set(warnings))),
            reason_codes=tuple(sorted(reason_codes)),
        )

    def _prepare_rows(
        self,
        recording: RecordingView,
        segment_id: SegmentId,
        segment_start_utc_ns: UtcNs,
        sample_rate_hz: float,
        receiver_count: int,
        frame_starts: tuple[int, ...],
        row_count: int,
    ) -> tuple[_PreparedRow, ...]:
        boundaries = tuple(
            index * len(frame_starts) // row_count for index in range(row_count + 1)
        )
        result: list[_PreparedRow] = []
        for row_index in range(row_count):
            starts = frame_starts[boundaries[row_index] : boundaries[row_index + 1]]
            per_receiver_frames: list[list[tuple[float, ...]]] = [
                [] for _ in range(receiver_count)
            ]
            for start in starts:
                raw = self._read_exact(
                    recording,
                    segment_id,
                    start,
                    start + self._config.fft_window_samples,
                    receiver_count,
                )
                try:
                    power = self._backend.frame_power(
                        raw,
                        receiver_count=receiver_count,
                        fft_window_samples=self._config.fft_window_samples,
                        display_frequency_bins=self._config.display_frequency_bins,
                    )
                    self._validate_backend_result(power, receiver_count)
                except Exception as error:
                    raise AnalysisInputError(
                        f"waterfall numerical backend failed for {segment_id}[{start}]"
                    ) from error
                for receiver_index, values in enumerate(power):
                    per_receiver_frames[receiver_index].append(values)
            averages: list[tuple[float, ...]] = []
            percentiles: list[tuple[float, ...]] = []
            for frames in per_receiver_frames:
                averages.append(
                    tuple(
                        _db(
                            sum(frame[index] for frame in frames) / len(frames),
                            self._config.power_floor_counts_squared,
                        )
                        for index in range(self._config.display_frequency_bins)
                    )
                )
                percentiles.append(
                    tuple(
                        _db(
                            _nearest_rank(
                                tuple(frame[index] for frame in frames),
                                self._config.high_percentile,
                            ),
                            self._config.power_floor_counts_squared,
                        )
                        for index in range(self._config.display_frequency_bins)
                    )
                )
            stop = starts[-1] + self._config.fft_window_samples
            result.append(
                _PreparedRow(
                    start_sample=starts[0],
                    stop_sample=stop,
                    midpoint_utc_ns=UtcNs(
                        segment_start_utc_ns
                        + round((starts[0] + stop) * 500_000_000 / sample_rate_hz)
                    ),
                    frame_starts=starts,
                    frame_count=len(starts),
                    receiver_average_db=tuple(averages),
                    receiver_percentile_db=tuple(percentiles),
                )
            )
        return tuple(result)

    def _validate_backend_result(self, power: object, receiver_count: int) -> None:
        if not isinstance(power, tuple) or len(power) != receiver_count:
            raise ValueError("backend receiver count is invalid")
        for row in power:
            if (
                not isinstance(row, tuple)
                or len(row) != self._config.display_frequency_bins
            ):
                raise ValueError("backend display frequency width is invalid")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in row
            ):
                raise ValueError("backend power must be finite and non-negative")

    @staticmethod
    def _validated_spans(
        recording: RecordingView, segment_id: SegmentId, segment_sample_count: int
    ) -> tuple[ContiguousRfSpan, ...]:
        try:
            spans = recording.contiguous_rf_spans(segment_id)
        except Exception as error:
            raise AnalysisInputError(
                f"recording continuity read failed for {segment_id}"
            ) from error
        if not isinstance(spans, tuple) or not spans:
            raise AnalysisInputError("recording returned no contiguous RF spans")
        previous_stop = 0
        for span in spans:
            start = getattr(span, "start_sample", None)
            stop = getattr(span, "stop_sample", None)
            if (
                isinstance(start, bool)
                or isinstance(stop, bool)
                or not isinstance(start, int)
                or not isinstance(stop, int)
                or start < previous_stop
                or not 0 <= start < stop <= segment_sample_count
            ):
                raise AnalysisInputError(
                    "recording returned invalid contiguous RF spans"
                )
            previous_stop = stop
        return spans

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
