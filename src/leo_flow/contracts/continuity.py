"""Normalized, immutable capture-continuity facts.

These contracts deliberately describe observations rather than the SPF/libiio
wire structs.  Capture adapters own translation from a particular host API.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .core import ReceiverChainId


class ContinuityPolicy(str, Enum):
    REQUIRE_CONTIGUOUS = "require_verified"
    REQUIRE_VERIFIED = "require_verified"  # noqa: PIE796 - compatibility alias
    ALLOW_VERIFIED_GAPPED = "allow_verified_gapped"
    ALLOW_UNVERIFIED = "allow_unverified"


class ContinuityStatus(str, Enum):
    VERIFIED_CONTIGUOUS = "verified_contiguous"
    VERIFIED = "verified_contiguous"  # noqa: PIE796 - compatibility alias
    VERIFIED_GAPPED = "verified_gapped"
    UNVERIFIED = "unverified"


class RefillFlag(str, Enum):
    DEVICE_IIO_OVERFLOW = "device_iio_overflow"
    GAIN_READ_FAILED = "gain_read_failed"
    RSSI_READ_FAILED = "rssi_read_failed"
    GAIN_OBSERVATION_OVERFLOW = "gain_observation_overflow"
    FPGA_EVENT_OVERFLOW = "fpga_event_overflow"
    METADATA_READ_FAILED = "metadata_read_failed"


@dataclass(frozen=True, slots=True)
class CaptureProvenance:
    firmware_release: str
    firmware_commit: str
    host_libiio_version: str
    metadata_protocol: str
    capability: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.firmware_release,
                self.firmware_commit,
                self.host_libiio_version,
                self.metadata_protocol,
                self.capability,
            )
        ):
            raise ValueError("capture provenance fields cannot be empty")


@dataclass(frozen=True, slots=True)
class GainObservation:
    sample_sequence_before: int
    sample_sequence_after: int
    read_duration_ns: int
    gain_db: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            self.sample_sequence_before < 0
            or self.sample_sequence_after < self.sample_sequence_before
            or self.read_duration_ns < 0
            or not self.gain_db
        ):
            raise ValueError("invalid gain observation")
        if not all(isfinite(value) for value in self.gain_db):
            raise ValueError("gain observations must be finite")


@dataclass(frozen=True, slots=True)
class RefillMetadata:
    """Facts associated with exactly one IQ refill.

    ``segment_sample_offset`` indexes the separately stored IQ object.  Sample
    sequences and times describe radio time and may therefore expose gaps.
    """

    refill_index: int
    segment_sample_offset: int
    sample_count: int
    stream_id: int
    buffer_sequence: int
    first_sample_sequence: int
    monotonic_start_ns: int
    monotonic_end_ns: int
    utc_start_ns: int
    utc_end_ns: int
    time_uncertainty_ns: int
    gain_db_start: tuple[float, ...]
    gain_db_end: tuple[float, ...]
    rssi_db_start: tuple[float, ...]
    rssi_db_end: tuple[float, ...]
    gain_observation_overflow_count: int = 0
    gain_event_overflow_count: int = 0
    gain_observations: tuple[GainObservation, ...] = ()
    flags: tuple[RefillFlag, ...] = ()

    def __post_init__(self) -> None:
        integers = (
            self.refill_index,
            self.segment_sample_offset,
            self.stream_id,
            self.buffer_sequence,
            self.first_sample_sequence,
            self.monotonic_start_ns,
            self.utc_start_ns,
            self.time_uncertainty_ns,
            self.gain_observation_overflow_count,
            self.gain_event_overflow_count,
        )
        if any(value < 0 for value in integers) or self.sample_count <= 0:
            raise ValueError("refill counters, offsets and times are invalid")
        if (
            self.monotonic_end_ns <= self.monotonic_start_ns
            or self.utc_end_ns <= self.utc_start_ns
        ):
            raise ValueError("refill time intervals must be non-empty")
        widths = {
            len(self.gain_db_start),
            len(self.gain_db_end),
            len(self.rssi_db_start),
            len(self.rssi_db_end),
        }
        if widths != {2}:
            raise ValueError("v5 refill endpoints require exactly two receivers")
        if not all(
            isfinite(value)
            for values in (
                self.gain_db_start,
                self.gain_db_end,
                self.rssi_db_start,
                self.rssi_db_end,
            )
            for value in values
        ):
            raise ValueError("gain and RSSI endpoints must be finite")
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("refill flags must be unique")

    @property
    def sample_sequence_end_exclusive(self) -> int:
        return self.first_sample_sequence + self.sample_count


@dataclass(frozen=True, slots=True)
class ContinuityGap:
    """A radio-time discontinuity at one exact stored-IQ boundary."""

    prior_refill_index: int
    next_refill_index: int
    stored_sample_offset: int
    first_missing_sample_sequence: int
    next_sample_sequence: int
    missing_sample_count: int
    missing_buffer_count: int

    def __post_init__(self) -> None:
        if (
            self.prior_refill_index < 0
            or self.next_refill_index != self.prior_refill_index + 1
            or self.stored_sample_offset <= 0
            or self.first_missing_sample_sequence < 0
            or self.next_sample_sequence <= self.first_missing_sample_sequence
            or self.missing_sample_count
            != self.next_sample_sequence - self.first_missing_sample_sequence
            or self.missing_buffer_count < 0
        ):
            raise ValueError("invalid continuity gap extent")
        if self.missing_sample_count <= 0 and self.missing_buffer_count <= 0:
            raise ValueError("continuity gap must describe missing source data")


@dataclass(frozen=True, slots=True)
class ContiguousRfSpan:
    """Stored sample interval proven contiguous in radio sample sequence."""

    start_sample: int
    stop_sample: int
    first_sample_sequence: int
    stop_sample_sequence: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_sample < self.stop_sample:
            raise ValueError("RF span stored range must be non-empty")
        if (
            self.first_sample_sequence < 0
            or self.stop_sample_sequence <= self.first_sample_sequence
            or self.stop_sample - self.start_sample
            != self.stop_sample_sequence - self.first_sample_sequence
        ):
            raise ValueError("RF span sample sequences differ from stored extent")


@dataclass(frozen=True, slots=True)
class SafeSampleWindow:
    start_sample: int
    stop_sample: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_sample < self.stop_sample:
            raise ValueError("safe sample window must be non-empty")


@dataclass(frozen=True, slots=True)
class SegmentContinuity:
    status: ContinuityStatus
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    provenance: CaptureProvenance
    refills: tuple[RefillMetadata, ...]
    gaps: tuple[ContinuityGap, ...] = ()

    def __post_init__(self) -> None:
        if len(self.receiver_chain_ids) != 2 or len(set(self.receiver_chain_ids)) != 2:
            raise ValueError("paired continuity requires two unique receiver chains")
        if self.status in {
            ContinuityStatus.VERIFIED_CONTIGUOUS,
            ContinuityStatus.VERIFIED_GAPPED,
        }:
            if not self.refills:
                raise ValueError("verified continuity requires refill metadata")
            derived = _verify_refills(self.refills)
            if self.status is ContinuityStatus.VERIFIED_CONTIGUOUS and derived:
                if any(gap.missing_buffer_count for gap in derived):
                    raise ValueError("capture buffer sequence gap")
                raise ValueError("hardware sample sequence gap")
            if self.status is ContinuityStatus.VERIFIED_GAPPED and not derived:
                raise ValueError("verified-gapped continuity requires a source gap")
            if self.gaps != derived:
                raise ValueError("declared continuity gaps differ from refill evidence")
        elif self.refills or self.gaps:
            raise ValueError("unverified continuity cannot contain trusted evidence")

    @classmethod
    def from_refills(
        cls,
        receiver_chain_ids: tuple[ReceiverChainId, ...],
        provenance: CaptureProvenance,
        refills: tuple[RefillMetadata, ...],
    ) -> SegmentContinuity:
        if not refills:
            return cls(
                ContinuityStatus.UNVERIFIED,
                receiver_chain_ids,
                provenance,
                (),
            )
        gaps = _verify_refills(refills)
        return cls(
            ContinuityStatus.VERIFIED_GAPPED
            if gaps
            else ContinuityStatus.VERIFIED_CONTIGUOUS,
            receiver_chain_ids,
            provenance,
            refills,
            gaps,
        )

    @property
    def is_verified(self) -> bool:
        return self.status is not ContinuityStatus.UNVERIFIED

    def contiguous_rf_spans(self) -> tuple[ContiguousRfSpan, ...]:
        if not self.is_verified:
            raise ValueError("unverified recording has no proven RF spans")
        boundaries = {gap.next_refill_index for gap in self.gaps}
        starts = [0]
        starts.extend(sorted(boundaries))
        stops = starts[1:] + [len(self.refills)]
        return tuple(
            ContiguousRfSpan(
                self.refills[start].segment_sample_offset,
                self.refills[stop - 1].segment_sample_offset
                + self.refills[stop - 1].sample_count,
                self.refills[start].first_sample_sequence,
                self.refills[stop - 1].sample_sequence_end_exclusive,
            )
            for start, stop in zip(starts, stops, strict=True)
        )

    def safe_windows(
        self, window_samples: int, stride_samples: int
    ) -> tuple[SafeSampleWindow, ...]:
        if (
            isinstance(window_samples, bool)
            or isinstance(stride_samples, bool)
            or window_samples <= 0
            or stride_samples <= 0
        ):
            raise ValueError("window and stride samples must be positive integers")
        windows: list[SafeSampleWindow] = []
        for span in self.contiguous_rf_spans():
            span_count = span.stop_sample - span.start_sample
            if span_count < window_samples:
                continue
            starts = list(
                range(
                    span.start_sample,
                    span.stop_sample - window_samples + 1,
                    stride_samples,
                )
            )
            last = span.stop_sample - window_samples
            if starts[-1] != last:
                starts.append(last)
            windows.extend(
                SafeSampleWindow(start, start + window_samples) for start in starts
            )
        return tuple(windows)


def _verify_refills(
    refills: tuple[RefillMetadata, ...],
) -> tuple[ContinuityGap, ...]:
    prior: RefillMetadata | None = None
    gaps: list[ContinuityGap] = []
    for expected_index, refill in enumerate(refills):
        if refill.refill_index != expected_index:
            raise ValueError("refill indexes must be consecutive from zero")
        if (
            refill.flags
            or refill.gain_observation_overflow_count
            or refill.gain_event_overflow_count
        ):
            raise ValueError("verified continuity cannot contain failure flags")
        if prior is None:
            if refill.segment_sample_offset != 0:
                raise ValueError("first refill must start at segment sample zero")
        else:
            if refill.stream_id != prior.stream_id:
                raise ValueError("stream identity changed within a segment")
            if refill.buffer_sequence <= prior.buffer_sequence:
                raise ValueError("capture buffer sequence regressed")
            if refill.first_sample_sequence < prior.sample_sequence_end_exclusive:
                raise ValueError("hardware sample sequence regressed or overlapped")
            if (
                refill.segment_sample_offset
                != prior.segment_sample_offset + prior.sample_count
            ):
                raise ValueError("stored IQ refill ranges have a gap or overlap")
            missing_buffers = refill.buffer_sequence - prior.buffer_sequence - 1
            missing_samples = (
                refill.first_sample_sequence - prior.sample_sequence_end_exclusive
            )
            if missing_buffers or missing_samples:
                if missing_samples <= 0:
                    raise ValueError(
                        "capture buffer sequence gap lacks a hardware sample sequence gap"
                    )
                gaps.append(
                    ContinuityGap(
                        prior_refill_index=prior.refill_index,
                        next_refill_index=refill.refill_index,
                        stored_sample_offset=refill.segment_sample_offset,
                        first_missing_sample_sequence=prior.sample_sequence_end_exclusive,
                        next_sample_sequence=refill.first_sample_sequence,
                        missing_sample_count=missing_samples,
                        missing_buffer_count=missing_buffers,
                    )
                )
            prior_end_lower_bound = prior.monotonic_end_ns - prior.time_uncertainty_ns
            current_start_upper_bound = (
                refill.monotonic_start_ns + refill.time_uncertainty_ns
            )
            if current_start_upper_bound < prior_end_lower_bound:
                raise ValueError("refill monotonic times contradict their uncertainty")
        prior = refill
    return tuple(gaps)
