"""Event-scripted paired-radio oracle for capture tests and development."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from leo_flow.contracts._validation import freeze_mapping
from leo_flow.contracts.capture import SegmentManifest, SegmentRequest
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
)
from leo_flow.contracts.core import RadioId, ReceiverChainId, SegmentId, UtcNs

from .clock import CaptureClock, SystemCaptureClock
from .errors import (
    RadioConfigurationError,
    RadioDisconnectedError,
    ReceiverSkewError,
    RefillError,
    SampleCountError,
    TuningError,
)

CI16_COMPONENT_BYTES = 2
IQ_COMPONENTS = 2


@dataclass(frozen=True)
class Refill:
    ci16_bytes: bytes


@dataclass(frozen=True)
class V5Refill:
    ci16_bytes: bytes
    metadata: RefillMetadata


@dataclass(frozen=True)
class ShortRead:
    """A valid, frame-aligned refill shorter than the device's normal block."""

    ci16_bytes: bytes


@dataclass(frozen=True)
class MissingRefill:
    """The driver returned no buffer for one refill attempt."""


@dataclass(frozen=True)
class ReceiverSkew:
    receiver_sample_counts: tuple[int, ...]


@dataclass(frozen=True)
class Delay:
    seconds: float

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise ValueError("delay cannot be negative")


@dataclass(frozen=True)
class TuningFailure:
    reason: str = "injected tuning failure"


@dataclass(frozen=True)
class Disconnect:
    reason: str = "injected radio disconnect"


RadioEvent: TypeAlias = (
    Refill
    | ShortRead
    | MissingRefill
    | ReceiverSkew
    | Delay
    | TuningFailure
    | Disconnect
)


class FakePairedRadio:
    """A deterministic `RadioDevice` with no SDR or physical-format dependency."""

    def __init__(
        self,
        radio_id: RadioId,
        receiver_chain_ids: tuple[ReceiverChainId, ...],
        scripts: Mapping[SegmentId, Sequence[RadioEvent]],
        *,
        clock: CaptureClock | None = None,
        delay: Callable[[float], None] = time.sleep,
        max_consecutive_missing_refills: int = 2,
    ) -> None:
        if len(receiver_chain_ids) != 2 or len(set(receiver_chain_ids)) != 2:
            raise ValueError("FakePairedRadio requires exactly two receiver chains")
        if max_consecutive_missing_refills < 0:
            raise ValueError("missing-refill tolerance cannot be negative")
        self._radio_id = radio_id
        self.receiver_chain_ids = receiver_chain_ids
        self._scripts = {
            segment_id: tuple(events) for segment_id, events in scripts.items()
        }
        self._clock = clock or SystemCaptureClock()
        self._delay = delay
        self._max_missing = max_consecutive_missing_refills
        self.acquired_segment_ids: list[SegmentId] = []

    @property
    def radio_id(self) -> RadioId:
        return self._radio_id

    def acquire_segment(
        self, request: SegmentRequest, write_ci16: Callable[[bytes], None]
    ) -> SegmentManifest:
        if request.receiver_chain_ids != self.receiver_chain_ids:
            raise RadioConfigurationError("request receiver order differs from radio")
        try:
            events = self._scripts[request.segment_id]
        except KeyError as error:
            raise RefillError(f"no event script for {request.segment_id}") from error

        target_samples = _requested_sample_count(request)
        frame_bytes = (
            len(request.receiver_chain_ids) * IQ_COMPONENTS * CI16_COMPONENT_BYTES
        )
        start_utc_ns = UtcNs(self._clock.now_utc_ns())
        monotonic_start_ns = self._clock.now_monotonic_ns()
        written_samples = 0
        short_reads = 0
        missing_refills = 0
        consecutive_missing = 0
        delay_seconds = 0.0

        for event in events:
            if isinstance(event, Delay):
                self._delay(event.seconds)
                delay_seconds += event.seconds
                continue
            if isinstance(event, TuningFailure):
                raise TuningError(event.reason)
            if isinstance(event, Disconnect):
                raise RadioDisconnectedError(event.reason)
            if isinstance(event, ReceiverSkew):
                if len(event.receiver_sample_counts) != len(request.receiver_chain_ids):
                    raise ReceiverSkewError("skew report has the wrong receiver count")
                if len(set(event.receiver_sample_counts)) != 1:
                    raise ReceiverSkewError(
                        f"receiver sample counts differ: {event.receiver_sample_counts}"
                    )
                continue
            if isinstance(event, MissingRefill):
                missing_refills += 1
                consecutive_missing += 1
                if consecutive_missing > self._max_missing:
                    raise RefillError("consecutive missing-refill limit exceeded")
                continue

            encoded = event.ci16_bytes
            if not encoded or len(encoded) % frame_bytes:
                raise SampleCountError(
                    "refill is empty or not a complete paired CI16 frame"
                )
            refill_samples = len(encoded) // frame_bytes
            if written_samples + refill_samples > target_samples:
                raise SampleCountError("refill exceeds requested sample count")
            write_ci16(encoded)
            written_samples += refill_samples
            consecutive_missing = 0
            if isinstance(event, ShortRead):
                short_reads += 1
            if written_samples == target_samples:
                break

        if written_samples != target_samples:
            raise SampleCountError(
                f"segment ended at {written_samples} of {target_samples} samples"
            )
        self.acquired_segment_ids.append(request.segment_id)
        diagnostics = freeze_mapping(
            {
                "delay_seconds": delay_seconds,
                "missing_refills": missing_refills,
                "short_reads": short_reads,
            },
            "diagnostics",
        )
        return SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=request.center_frequency_hz,
            actual_sample_rate_hz=request.sample_rate_hz,
            actual_bandwidth_hz=request.bandwidth_hz,
            actual_gain=request.gain,
            start_utc_ns=start_utc_ns,
            monotonic_start_ns=monotonic_start_ns,
            sample_count=written_samples,
            shape=(written_samples, len(request.receiver_chain_ids), IQ_COMPONENTS),
            diagnostics=diagnostics,
        )


def _requested_sample_count(request: SegmentRequest) -> int:
    if request.sample_count is not None:
        return request.sample_count
    assert request.duration_s is not None
    samples = round(request.duration_s * request.sample_rate_hz)
    if samples <= 0:
        raise SampleCountError("duration resolves to no samples")
    return samples


class FakeV5PairedRadio:
    """Strict metadata-aware paired radio for hardware-free capture tests."""

    def __init__(
        self,
        radio_id: RadioId,
        receiver_chain_ids: tuple[ReceiverChainId, ReceiverChainId],
        scripts: Mapping[SegmentId, Sequence[V5Refill]],
        provenance: CaptureProvenance,
        *,
        continuity_policy: ContinuityPolicy = ContinuityPolicy.REQUIRE_VERIFIED,
        clock: CaptureClock | None = None,
    ) -> None:
        if len(set(receiver_chain_ids)) != 2:
            raise ValueError("FakeV5PairedRadio requires two receiver chains")
        self._radio_id = radio_id
        self.receiver_chain_ids = receiver_chain_ids
        self._scripts = {key: tuple(value) for key, value in scripts.items()}
        self._provenance = provenance
        self._policy = continuity_policy
        self._clock = clock or SystemCaptureClock()

    @property
    def radio_id(self) -> RadioId:
        return self._radio_id

    @property
    def continuity_policy(self) -> ContinuityPolicy:
        return self._policy

    @property
    def capture_provenance(self) -> CaptureProvenance:
        return self._provenance

    def acquire_segment_with_metadata(
        self,
        request: SegmentRequest,
        write_refill: Callable[[bytes, RefillMetadata | None], None],
    ) -> SegmentManifest:
        if request.receiver_chain_ids != self.receiver_chain_ids:
            raise RadioConfigurationError("request receiver order differs from radio")
        events = self._scripts.get(request.segment_id, ())
        target_samples = _requested_sample_count(request)
        frame_bytes = (
            len(self.receiver_chain_ids) * IQ_COMPONENTS * CI16_COMPONENT_BYTES
        )
        start_utc_ns = UtcNs(self._clock.now_utc_ns())
        monotonic_start_ns = self._clock.now_monotonic_ns()
        written = 0
        for event in events:
            if not event.ci16_bytes or len(event.ci16_bytes) % frame_bytes:
                raise SampleCountError("v5 refill is not paired CI16")
            samples = len(event.ci16_bytes) // frame_bytes
            if event.metadata.sample_count != samples:
                raise RefillError("v5 metadata sample count differs from IQ")
            if written + samples > target_samples:
                raise SampleCountError("v5 refill exceeds requested sample count")
            write_refill(event.ci16_bytes, event.metadata)
            written += samples
        if written != target_samples:
            raise SampleCountError(
                f"segment ended at {written} of {target_samples} samples"
            )
        return SegmentManifest(
            segment_id=request.segment_id,
            requested=request,
            actual_center_frequency_hz=request.center_frequency_hz,
            actual_sample_rate_hz=request.sample_rate_hz,
            actual_bandwidth_hz=request.bandwidth_hz,
            actual_gain=request.gain,
            start_utc_ns=start_utc_ns,
            monotonic_start_ns=monotonic_start_ns,
            sample_count=written,
            shape=(written, 2, IQ_COMPONENTS),
            diagnostics=freeze_mapping(
                {"continuity": "verified", "refill_count": len(events)},
                "diagnostics",
            ),
        )
