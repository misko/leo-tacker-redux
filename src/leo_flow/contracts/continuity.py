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
    REQUIRE_VERIFIED = "require_verified"
    ALLOW_UNVERIFIED = "allow_unverified"


class ContinuityStatus(str, Enum):
    VERIFIED = "verified"
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
class SegmentContinuity:
    status: ContinuityStatus
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    provenance: CaptureProvenance
    refills: tuple[RefillMetadata, ...]

    def __post_init__(self) -> None:
        if len(self.receiver_chain_ids) != 2 or len(set(self.receiver_chain_ids)) != 2:
            raise ValueError("paired continuity requires two unique receiver chains")
        if self.status is ContinuityStatus.VERIFIED:
            if not self.refills:
                raise ValueError("verified continuity requires refill metadata")
            _verify_refills(self.refills)
        elif self.refills:
            raise ValueError("unverified continuity cannot contain trusted refills")


def _verify_refills(refills: tuple[RefillMetadata, ...]) -> None:
    prior: RefillMetadata | None = None
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
            if refill.buffer_sequence != prior.buffer_sequence + 1:
                raise ValueError("capture buffer sequence gap")
            if refill.first_sample_sequence != prior.sample_sequence_end_exclusive:
                raise ValueError("hardware sample sequence gap")
            if (
                refill.segment_sample_offset
                != prior.segment_sample_offset + prior.sample_count
            ):
                raise ValueError("stored IQ refill ranges have a gap or overlap")
            if refill.monotonic_start_ns < prior.monotonic_end_ns:
                raise ValueError("refill monotonic times overlap")
        prior = refill
