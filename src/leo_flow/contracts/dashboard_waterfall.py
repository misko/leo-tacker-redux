"""Versioned, bounded waterfall projections for the read-only dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    V0_1,
    AnalysisRunId,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)

MAX_WATERFALL_TILES = 64
MAX_WATERFALL_TIME_BINS = 128
MAX_WATERFALL_FREQUENCY_BINS = 128
MAX_WATERFALL_CELLS = 262_144
MAX_WATERFALL_JSON_BYTES = 4 * 1024 * 1024


class WaterfallProjectionState(str, Enum):
    UNAVAILABLE = "unavailable"
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class WaterfallTileViewV0_1:
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    segment_start_utc_ns: UtcNs
    segment_sample_count: int
    center_frequency_hz: float
    sample_rate_hz: float
    fft_window_samples: int
    time_bin_start_samples: tuple[int, ...]
    time_bin_stop_samples: tuple[int, ...]
    time_bin_midpoint_utc_ns: tuple[UtcNs, ...]
    frequency_bin_offsets_hz: tuple[float, ...]
    power_db: tuple[tuple[float, ...], ...]
    power_reference: str
    floor_db: float
    ceiling_db: float

    def __post_init__(self) -> None:
        require_utc_ns(self.segment_start_utc_ns, "segment_start_utc_ns")
        for name in ("segment_sample_count", "fft_window_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            self.fft_window_samples < 8
            or self.fft_window_samples > self.segment_sample_count
            or self.fft_window_samples & (self.fft_window_samples - 1)
        ):
            raise ValueError(
                "fft_window_samples must be a power of two within the segment"
            )
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        require_token(self.power_reference, "power_reference")
        require_finite(self.floor_db, "floor_db")
        require_finite(self.ceiling_db, "ceiling_db")
        if self.ceiling_db <= self.floor_db:
            raise ValueError("ceiling_db must exceed floor_db")
        rows = len(self.power_db)
        columns = len(self.frequency_bin_offsets_hz)
        if not 1 <= rows <= MAX_WATERFALL_TIME_BINS:
            raise ValueError("waterfall tile time-bin count is out of bounds")
        if not 1 <= columns <= MAX_WATERFALL_FREQUENCY_BINS:
            raise ValueError("waterfall tile frequency-bin count is out of bounds")
        if not (
            len(self.time_bin_start_samples)
            == len(self.time_bin_stop_samples)
            == len(self.time_bin_midpoint_utc_ns)
            == rows
        ):
            raise ValueError("waterfall time axes must match the power row count")
        if any(len(row) != columns for row in self.power_db):
            raise ValueError("waterfall power rows must match the frequency axis")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.frequency_bin_offsets_hz,
                self.frequency_bin_offsets_hz[1:],
                strict=False,
            )
        ):
            raise ValueError("frequency offsets must be strictly increasing")
        for offset in self.frequency_bin_offsets_hz:
            require_finite(offset, "frequency_bin_offsets_hz")
            if not -self.sample_rate_hz / 2 <= offset < self.sample_rate_hz / 2:
                raise ValueError("frequency offset is outside Nyquist")
        for index, (start, stop, midpoint) in enumerate(
            zip(
                self.time_bin_start_samples,
                self.time_bin_stop_samples,
                self.time_bin_midpoint_utc_ns,
                strict=True,
            )
        ):
            if (
                isinstance(start, bool)
                or isinstance(stop, bool)
                or not isinstance(start, int)
                or not isinstance(stop, int)
                or start < 0
                or stop <= start
                or stop > self.segment_sample_count
            ):
                raise ValueError("waterfall sample intervals are invalid")
            if stop - start != self.fft_window_samples:
                raise ValueError("waterfall sample interval must equal the FFT window")
            if index and start < self.time_bin_stop_samples[index - 1]:
                raise ValueError("waterfall sample intervals must not overlap")
            require_utc_ns(midpoint, "time_bin_midpoint_utc_ns")
        for row in self.power_db:
            for value in row:
                require_finite(value, "power_db")


@dataclass(frozen=True)
class RecordingWaterfallViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    recording_identity_digest: Digest
    analysis_run_id: AnalysisRunId | None
    state: WaterfallProjectionState
    reason_code: str | None
    tiles: tuple[WaterfallTileViewV0_1, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-waterfall"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported recording waterfall schema")
        if not isinstance(self.state, WaterfallProjectionState):
            raise TypeError("state must be a WaterfallProjectionState")
        if len(self.tiles) > MAX_WATERFALL_TILES:
            raise ValueError("waterfall projection has too many tiles")
        if (
            sum(
                len(tile.power_db) * len(tile.frequency_bin_offsets_hz)
                for tile in self.tiles
            )
            > MAX_WATERFALL_CELLS
        ):
            raise ValueError("waterfall projection has too many cells")
        identities = tuple(
            (tile.segment_id, tile.receiver_chain_id) for tile in self.tiles
        )
        if len(set(identities)) != len(identities):
            raise ValueError("waterfall segment/receiver tiles must be unique")
        if (
            tuple(sorted(identities, key=lambda item: (str(item[0]), str(item[1]))))
            != identities
        ):
            raise ValueError("waterfall tiles must use canonical identity order")
        if self.state is WaterfallProjectionState.COMPLETE:
            if (
                self.analysis_run_id is None
                or self.reason_code is not None
                or not self.tiles
            ):
                raise ValueError(
                    "complete waterfall requires a run and non-empty tiles"
                )
        elif self.state is WaterfallProjectionState.FAILED:
            if self.analysis_run_id is None or self.reason_code is None or self.tiles:
                raise ValueError(
                    "failed waterfall requires a run, reason, and no tiles"
                )
            require_token(self.reason_code, "reason_code")
        elif self.state is WaterfallProjectionState.PENDING:
            if (
                self.analysis_run_id is None
                or self.reason_code is not None
                or self.tiles
            ):
                raise ValueError("pending waterfall requires a run and no result")
        elif (
            self.analysis_run_id is not None
            or self.reason_code is not None
            or self.tiles
        ):
            raise ValueError("unavailable waterfall cannot identify analysis output")
        if len(canonical_json_bytes(self)) > MAX_WATERFALL_JSON_BYTES:
            raise ValueError("waterfall projection exceeds its JSON byte bound")


class RecordingWaterfallQueryPortV0_1(Protocol):
    """Return a bounded projection; implementations never expose raw IQ."""

    def recording_waterfall(
        self, recording_id: RecordingId
    ) -> RecordingWaterfallViewV0_1: ...
