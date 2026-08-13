from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityStatus,
    GainObservation,
    RefillFlag,
    RefillMetadata,
    SegmentContinuity,
)
from leo_flow.contracts.core import ReceiverChainId


def provenance() -> CaptureProvenance:
    return CaptureProvenance(
        "v0.38-plutoplus-spf-libiio-metadata-v5",
        "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
        "0.25+c26258b",
        "spf-radio-metadata-v3",
        "iio,buffer-metadata=1",
    )


def refill(index: int = 0, *, sequence: int = 100, flags=()) -> RefillMetadata:
    offset = index * 4
    return RefillMetadata(
        refill_index=index,
        segment_sample_offset=offset,
        sample_count=4,
        stream_id=7,
        buffer_sequence=20 + index,
        first_sample_sequence=sequence,
        monotonic_start_ns=1_000 + index * 100,
        monotonic_end_ns=1_050 + index * 100,
        utc_start_ns=1_700_000_000_000_001_000 + index * 100,
        utc_end_ns=1_700_000_000_000_001_050 + index * 100,
        time_uncertainty_ns=10,
        gain_db_start=(40.0, 41.0),
        gain_db_end=(41.0, 42.0),
        rssi_db_start=(50.0, 51.0),
        rssi_db_end=(49.0, 50.0),
        gain_observations=(GainObservation(sequence, sequence + 1, 25, (40.0, 41.0)),),
        flags=flags,
    )


def test_verified_refills_are_immutable_and_contiguous() -> None:
    first = refill()
    second = refill(1, sequence=104)
    value = SegmentContinuity(
        ContinuityStatus.VERIFIED,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        provenance(),
        (first, second),
    )
    assert value.refills[-1].sample_sequence_end_exclusive == 108
    with pytest.raises(FrozenInstanceError):
        first.sample_count = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad_second,match",
    [
        (refill(1, sequence=105), "sample sequence gap"),
        (replace(refill(1, sequence=104), buffer_sequence=23), "buffer sequence gap"),
        (
            replace(refill(1, sequence=104), flags=(RefillFlag.DEVICE_IIO_OVERFLOW,)),
            "failure flags",
        ),
        (
            replace(refill(1, sequence=104), gain_observation_overflow_count=1),
            "failure flags",
        ),
    ],
)
def test_verified_continuity_rejects_gaps_and_overflow(bad_second, match) -> None:
    with pytest.raises(ValueError, match=match):
        SegmentContinuity(
            ContinuityStatus.VERIFIED,
            (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
            provenance(),
            (refill(), bad_second),
        )


def test_unverified_fallback_is_explicit_and_cannot_carry_trusted_records() -> None:
    value = SegmentContinuity(
        ContinuityStatus.UNVERIFIED,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        provenance(),
        (),
    )
    assert value.status is ContinuityStatus.UNVERIFIED
    with pytest.raises(ValueError, match="cannot contain trusted"):
        replace(value, refills=(refill(),))


def test_timestamp_fit_overlap_is_allowed_only_within_reported_uncertainty() -> None:
    first = refill()
    within_uncertainty = replace(
        refill(1, sequence=104),
        monotonic_start_ns=1_045,
        monotonic_end_ns=1_145,
    )
    SegmentContinuity(
        ContinuityStatus.VERIFIED,
        (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
        provenance(),
        (first, within_uncertainty),
    )

    contradicted = replace(
        within_uncertainty,
        monotonic_start_ns=1_020,
        monotonic_end_ns=1_120,
    )
    with pytest.raises(ValueError, match="contradict their uncertainty"):
        SegmentContinuity(
            ContinuityStatus.VERIFIED,
            (ReceiverChainId("rx_a"), ReceiverChainId("rx_b")),
            provenance(),
            (first, contradicted),
        )
