from __future__ import annotations

from pathlib import Path

import pytest

from leo_flow.contracts.capture import ActivityKind, SegmentManifest
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityStatus,
    RefillMetadata,
    SegmentContinuity,
)
from leo_flow.contracts.core import UtcNs, canonical_digest
from leo_flow.deployments.v5_dwell_e2e import (
    BLOCK_SAMPLES,
    DWELL_PLAN,
    DWELL_PLAN_DIGEST,
    EXPECTED_SERIAL,
    LONG_DWELL_PLAN,
    LONG_DWELL_PLAN_DIGEST,
    LONG_REFILL_COUNT,
    LONG_SAMPLE_COUNT,
    PLAN_ID,
    REFILL_COUNT,
    SAMPLE_COUNT,
    V5DwellE2EError,
    require_empty_output_root,
    require_live_confirmation,
    sustained_continuity_evidence,
)


def segment_manifest() -> SegmentManifest:
    request = DWELL_PLAN.activities[0].segments[0]
    return SegmentManifest(
        segment_id=request.segment_id,
        requested=request,
        actual_center_frequency_hz=1_825_117_187.0,
        actual_sample_rate_hz=2_083_331.0,
        actual_bandwidth_hz=2_000_000.0,
        actual_gain=request.gain,
        start_utc_ns=UtcNs(1_700_000_000_000_000_000),
        monotonic_start_ns=1_000_000_000,
        sample_count=SAMPLE_COUNT,
        shape=(SAMPLE_COUNT, 2, 2),
    )


def continuity(*, refill_count: int = REFILL_COUNT, gap_at: int | None = None):
    refills: list[RefillMetadata] = []
    for index in range(refill_count):
        skipped = 1 if gap_at is not None and index >= gap_at else 0
        monotonic_start = 1_000_000_000 + index * 126_000_000
        refills.append(
            RefillMetadata(
                refill_index=index,
                segment_sample_offset=index * BLOCK_SAMPLES,
                sample_count=BLOCK_SAMPLES,
                stream_id=700,
                buffer_sequence=100 + index + skipped,
                first_sample_sequence=(index + skipped) * BLOCK_SAMPLES,
                monotonic_start_ns=monotonic_start,
                monotonic_end_ns=monotonic_start + 125_000_000,
                utc_start_ns=1_700_000_000_000_000_000 + monotonic_start,
                utc_end_ns=(1_700_000_000_000_000_000 + monotonic_start + 125_000_000),
                time_uncertainty_ns=100_000,
                gain_db_start=(40.0, 41.0),
                gain_db_end=(40.0, 41.0),
                rssi_db_start=(-50.0, -51.0),
                rssi_db_end=(-50.0, -51.0),
            )
        )
    return SegmentContinuity.from_refills(
        DWELL_PLAN.receiver_chain_ids,
        CaptureProvenance(
            "v0.38-plutoplus-spf-libiio-metadata-v5",
            "firmware-commit",
            "0.25.c26258b",
            "spf-radio-metadata-v3",
            "iio,buffer-metadata=1",
        ),
        tuple(refills),
    )


def test_plan_is_one_pinned_receive_only_sixteen_refill_dwell() -> None:
    assert DWELL_PLAN.plan_id == PLAN_ID
    assert canonical_digest(DWELL_PLAN) == DWELL_PLAN_DIGEST
    assert len(DWELL_PLAN.activities) == 1
    assert DWELL_PLAN.activities[0].kind is ActivityKind.DWELL
    assert len(DWELL_PLAN.activities[0].segments) == 1
    request = DWELL_PLAN.activities[0].segments[0]
    assert request.sample_count == SAMPLE_COUNT == REFILL_COUNT * BLOCK_SAMPLES
    assert request.sample_rate_hz == 2_083_332.0
    assert dict(request.tags)["tx"] == "prohibited"


def test_long_plan_is_one_pinned_receive_only_256_refill_dwell() -> None:
    assert canonical_digest(LONG_DWELL_PLAN) == LONG_DWELL_PLAN_DIGEST
    assert len(LONG_DWELL_PLAN.activities) == 1
    assert LONG_DWELL_PLAN.activities[0].kind is ActivityKind.DWELL
    request = LONG_DWELL_PLAN.activities[0].segments[0]
    assert (
        request.sample_count == LONG_SAMPLE_COUNT == LONG_REFILL_COUNT * BLOCK_SAMPLES
    )
    assert dict(request.tags)["tx"] == "prohibited"


def test_sustained_evidence_requires_all_exact_same_stream_transitions() -> None:
    evidence = sustained_continuity_evidence(segment_manifest(), continuity())
    assert evidence["continuity_status"] == "verified_contiguous"
    assert evidence["refill_count"] == 16
    assert evidence["transition_count"] == 15
    assert evidence["stored_sample_count"] == SAMPLE_COUNT
    assert evidence["first_buffer_sequence"] == 100
    assert evidence["last_buffer_sequence"] == 115
    assert evidence["buffer_sequence_deltas"] == [1] * 15
    assert evidence["sample_sequence_deltas"] == [BLOCK_SAMPLES] * 15
    assert evidence["gap_count"] == 0
    assert evidence["missing_buffer_count"] == 0
    assert evidence["missing_sample_count"] == 0
    assert evidence["flags"] == []
    assert evidence["refill_elapsed_ns"] == {
        "count": 16,
        "min": 125_000_000,
        "median": 125_000_000.0,
        "p95": 125_000_000,
        "max": 125_000_000,
    }
    assert evidence["refill_start_period_ns"] == {
        "count": 15,
        "min": 126_000_000,
        "median": 126_000_000,
        "p95": 126_000_000,
        "max": 126_000_000,
    }
    assert evidence["refill_boundary_gap_ns"] == {
        "count": 15,
        "min": 1_000_000,
        "median": 1_000_000,
        "p95": 1_000_000,
        "max": 1_000_000,
    }
    assert evidence["time_uncertainty_ns"] == {
        "count": 16,
        "min": 100_000,
        "median": 100_000.0,
        "p95": 100_000,
        "max": 100_000,
    }


def test_long_evidence_accepts_exact_256_refill_series() -> None:
    request = LONG_DWELL_PLAN.activities[0].segments[0]
    segment = SegmentManifest(
        segment_id=request.segment_id,
        requested=request,
        actual_center_frequency_hz=1_825_117_187.0,
        actual_sample_rate_hz=2_083_331.0,
        actual_bandwidth_hz=2_000_000.0,
        actual_gain=request.gain,
        start_utc_ns=UtcNs(1_700_000_000_000_000_000),
        monotonic_start_ns=1_000_000_000,
        sample_count=LONG_SAMPLE_COUNT,
        shape=(LONG_SAMPLE_COUNT, 2, 2),
    )
    evidence = sustained_continuity_evidence(
        segment,
        continuity(refill_count=LONG_REFILL_COUNT),
        expected_refill_count=LONG_REFILL_COUNT,
    )
    assert evidence["refill_count"] == LONG_REFILL_COUNT
    assert evidence["transition_count"] == LONG_REFILL_COUNT - 1


def test_sustained_evidence_rejects_short_or_gapped_series() -> None:
    with pytest.raises(V5DwellE2EError, match="exact bounded refill count"):
        sustained_continuity_evidence(
            segment_manifest(), continuity(refill_count=REFILL_COUNT - 1)
        )
    gapped = continuity(gap_at=8)
    assert gapped.status is ContinuityStatus.VERIFIED_GAPPED
    with pytest.raises(V5DwellE2EError, match="not verified contiguous"):
        sustained_continuity_evidence(segment_manifest(), gapped)


def test_live_confirmation_and_output_root_fail_closed(tmp_path: Path) -> None:
    require_live_confirmation(EXPECTED_SERIAL)
    with pytest.raises(V5DwellE2EError, match="exact expected"):
        require_live_confirmation("other-radio")

    root = tmp_path / "dwell"
    assert require_empty_output_root(root) == root.resolve()
    (root / "existing").write_text("preserve", encoding="utf-8")
    with pytest.raises(V5DwellE2EError, match="absent or empty"):
        require_empty_output_root(root)
