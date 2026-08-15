"""Supervised receive-only sustained-dwell E2E for the qualified V5 radio."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median
from types import MappingProxyType

from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
    CapturePlan,
    GainMode,
    GainSetting,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.continuity import ContinuityStatus, SegmentContinuity
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    StationId,
    canonical_digest,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.deployments.v5_canary import V5RadioProvider
from leo_flow.deployments.v5_live_safety import verify_tx2_muted
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)

EXPECTED_URI = "ip:192.168.1.15"
EXPECTED_SERIAL = "104000b29905000e17000800065934759d"
REPORT_SCHEMA = "org.leo-flow.v5-dwell-e2e-report/v1"
BLOCK_SAMPLES = 262_144
REFILL_COUNT = 16
SAMPLE_COUNT = BLOCK_SAMPLES * REFILL_COUNT
LONG_REFILL_COUNT = 256
LONG_SAMPLE_COUNT = BLOCK_SAMPLES * LONG_REFILL_COUNT
BYTES_PER_PAIRED_SAMPLE = 2 * 2 * 2

RADIO_ID = RadioId("radio_pluto_v5_canary_15")
RECEIVER_CHAINS = (ReceiverChainId("rx_v5_1"), ReceiverChainId("rx_v5_2"))
PLAN_ID = PlanId("plan_v5_dwell_20260814_v1")
DWELL_PLAN = CapturePlan(
    schema=SchemaRef(CapturePlan.SCHEMA_ID),
    plan_id=PLAN_ID,
    radio_id=RADIO_ID,
    receiver_chain_ids=RECEIVER_CHAINS,
    activities=(
        ActivityRequest(
            ActivityId("act_v5_dwell_20260814_v1"),
            ActivityKind.DWELL,
            (
                SegmentRequest.create(
                    segment_id=SegmentId("seg_v5_dwell_20260814_v1"),
                    center_frequency_hz=1_825_117_187.5,
                    sample_rate_hz=2_083_332.0,
                    bandwidth_hz=2_000_000.0,
                    receiver_chain_ids=RECEIVER_CHAINS,
                    gain=GainSetting(GainMode.AGC),
                    sample_count=SAMPLE_COUNT,
                    tags={
                        "purpose": "passive-v5-rx-sustained-continuity-dwell",
                        "tx": "prohibited",
                        "ground_truth": "no-lnb-baseline-unknown-signal",
                    },
                ),
            ),
        ),
    ),
    experiment_tags=(
        ("fixture", "rx1-rx2-to-tx2-sma-tee-no-lnb"),
        ("purpose", "receive-only-same-stream-continuity"),
        ("refill_count", REFILL_COUNT),
    ),
)
DWELL_PLAN_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "bf55e04a414a3fed045d0f4aa627c10fc3fcbb66882e50f4841e7ae93d86054e",
)
if canonical_digest(DWELL_PLAN) != DWELL_PLAN_DIGEST:
    raise RuntimeError("embedded V5 dwell plan differs from its immutable digest")
LONG_PLAN_ID = PlanId("plan_v5_dwell_20260814_v2_long32s")
LONG_DWELL_PLAN = CapturePlan(
    schema=SchemaRef(CapturePlan.SCHEMA_ID),
    plan_id=LONG_PLAN_ID,
    radio_id=RADIO_ID,
    receiver_chain_ids=RECEIVER_CHAINS,
    activities=(
        ActivityRequest(
            ActivityId("act_v5_dwell_20260814_v2_long32s"),
            ActivityKind.DWELL,
            (
                SegmentRequest.create(
                    segment_id=SegmentId("seg_v5_dwell_20260814_v2_long32s"),
                    center_frequency_hz=1_825_117_187.5,
                    sample_rate_hz=2_083_332.0,
                    bandwidth_hz=2_000_000.0,
                    receiver_chain_ids=RECEIVER_CHAINS,
                    gain=GainSetting(GainMode.AGC),
                    sample_count=LONG_SAMPLE_COUNT,
                    tags={
                        "purpose": "passive-v5-rx-long-continuity-dwell",
                        "tx": "prohibited",
                        "ground_truth": "no-lnb-baseline-unknown-signal",
                    },
                ),
            ),
        ),
    ),
    experiment_tags=(
        ("fixture", "rx1-rx2-to-tx2-sma-tee-no-lnb"),
        ("purpose", "receive-only-same-stream-long-continuity"),
        ("refill_count", LONG_REFILL_COUNT),
    ),
)
LONG_DWELL_PLAN_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "909082cf80b02c5b67d9b8848e25d7d9fa33553cc734bf1d2938f1a314b0a64c",
)
if canonical_digest(LONG_DWELL_PLAN) != LONG_DWELL_PLAN_DIGEST:
    raise RuntimeError("embedded long V5 dwell plan differs from its immutable digest")
CAPTURE_IDENTITY = CaptureIdentity(
    StationId("station_leo_primary"),
    EXPECTED_SERIAL,
    "system-realtime-v5-metadata",
    HardwareSnapshotId("hw_v5_canary_20260814_v2"),
    "leo-flow-v5-dwell-e2e-v1",
)


class V5DwellE2EError(RuntimeError):
    """The bounded receive dwell failed a safety or completeness gate."""


@dataclass(frozen=True)
class DwellProfile:
    """One named, immutable live-radio dwell selection."""

    name: str
    plan: CapturePlan
    digest: Digest
    refill_count: int


SHORT_DWELL_PROFILE = DwellProfile(
    "short16", DWELL_PLAN, DWELL_PLAN_DIGEST, REFILL_COUNT
)
LONG_DWELL_PROFILE = DwellProfile(
    "long256", LONG_DWELL_PLAN, LONG_DWELL_PLAN_DIGEST, LONG_REFILL_COUNT
)
DWELL_PROFILES = MappingProxyType(
    {profile.name: profile for profile in (SHORT_DWELL_PROFILE, LONG_DWELL_PROFILE)}
)


def require_empty_output_root(path: Path) -> Path:
    if not path.is_absolute():
        raise V5DwellE2EError("output root must be absolute")
    root = path.resolve()
    if root.exists() and not root.is_dir():
        raise V5DwellE2EError("output root must be a directory")
    if root.exists() and any(root.iterdir()):
        raise V5DwellE2EError("output root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def require_live_confirmation(value: str) -> None:
    if value != EXPECTED_SERIAL:
        raise V5DwellE2EError(
            "live dwell requires the exact expected radio serial as confirmation"
        )


def sustained_continuity_evidence(
    segment: SegmentManifest,
    continuity: SegmentContinuity,
    *,
    expected_refill_count: int = REFILL_COUNT,
) -> dict[str, object]:
    """Fail unless every expected refill forms one exact contiguous V5 stream."""

    refills = continuity.refills
    if continuity.status is not ContinuityStatus.VERIFIED_CONTIGUOUS:
        raise V5DwellE2EError("dwell continuity is not verified contiguous")
    if len(refills) != expected_refill_count:
        raise V5DwellE2EError("dwell did not return the exact bounded refill count")
    if any(refill.sample_count != BLOCK_SAMPLES for refill in refills):
        raise V5DwellE2EError("dwell refill sample count changed")
    if sum(refill.sample_count for refill in refills) != segment.sample_count:
        raise V5DwellE2EError("dwell refill coverage differs from manifest")
    streams = {refill.stream_id for refill in refills}
    if len(streams) != 1:
        raise V5DwellE2EError("dwell stream identity changed")
    expected_buffers = list(
        range(
            refills[0].buffer_sequence,
            refills[0].buffer_sequence + expected_refill_count,
        )
    )
    buffers = [refill.buffer_sequence for refill in refills]
    if buffers != expected_buffers:
        raise V5DwellE2EError("dwell buffer sequences are not consecutive")
    sample_deltas = [
        current.first_sample_sequence - prior.first_sample_sequence
        for prior, current in pairwise(refills)
    ]
    if sample_deltas != [BLOCK_SAMPLES] * (expected_refill_count - 1):
        raise V5DwellE2EError("dwell sample sequences are not exact block increments")
    if continuity.gaps:
        raise V5DwellE2EError("dwell contains declared continuity gaps")
    flags = sorted({flag.value for refill in refills for flag in refill.flags})
    gain_overflows = sum(refill.gain_observation_overflow_count for refill in refills)
    event_overflows = sum(refill.gain_event_overflow_count for refill in refills)
    if flags or gain_overflows or event_overflows:
        raise V5DwellE2EError("dwell metadata contains flags or overflow counts")
    refill_elapsed_ns = [
        refill.monotonic_end_ns - refill.monotonic_start_ns for refill in refills
    ]
    refill_start_period_ns = [
        current.monotonic_start_ns - prior.monotonic_start_ns
        for prior, current in pairwise(refills)
    ]
    refill_boundary_gap_ns = [
        current.monotonic_start_ns - prior.monotonic_end_ns
        for prior, current in pairwise(refills)
    ]
    return {
        "continuity_status": continuity.status.value,
        "stream_id": next(iter(streams)),
        "refill_count": len(refills),
        "transition_count": len(refills) - 1,
        "stored_sample_count": sum(refill.sample_count for refill in refills),
        "samples_per_refill": BLOCK_SAMPLES,
        "first_buffer_sequence": buffers[0],
        "last_buffer_sequence": buffers[-1],
        "buffer_sequence_deltas": [
            current - prior for prior, current in pairwise(buffers)
        ],
        "first_sample_sequence": refills[0].first_sample_sequence,
        "last_sample_sequence_end_exclusive": refills[-1].sample_sequence_end_exclusive,
        "sample_sequence_deltas": sample_deltas,
        "gap_count": 0,
        "missing_buffer_count": 0,
        "missing_sample_count": 0,
        "flags": flags,
        "gain_observation_overflow_count": gain_overflows,
        "gain_event_overflow_count": event_overflows,
        "metadata_span_ns": refills[-1].monotonic_end_ns
        - refills[0].monotonic_start_ns,
        "refill_elapsed_ns": _distribution(refill_elapsed_ns),
        "refill_start_period_ns": _distribution(refill_start_period_ns),
        "refill_boundary_gap_ns": _distribution(refill_boundary_gap_ns),
        "time_uncertainty_ns": _distribution(
            [refill.time_uncertainty_ns for refill in refills]
        ),
    }


def _distribution(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }


def object_integrity_evidence(
    blobs: FileSystemBlobStore,
    recording: RecordingObjectRef,
    segment: SegmentManifest,
) -> dict[str, object]:
    """Re-hash the local recording pair and prove the exact paired IQ extent."""

    data = blobs.head(recording.data_object)
    metadata = blobs.head(recording.metadata_object)
    expected_bytes = segment.sample_count * BYTES_PER_PAIRED_SAMPLE
    if (
        not data.verified
        or not metadata.verified
        or recording.data_object.byte_count != expected_bytes
    ):
        raise V5DwellE2EError("local dwell object integrity or extent failed")
    return {
        "data": {
            "digest": str(recording.data_object.digest),
            "byte_count": recording.data_object.byte_count,
            "verified": data.verified,
        },
        "metadata": {
            "digest": str(recording.metadata_object.digest),
            "byte_count": recording.metadata_object.byte_count,
            "verified": metadata.verified,
        },
        "manifest_digest": str(recording.manifest_digest),
        "recording_identity_digest": str(recording.identity_digest()),
        "expected_data_byte_count": expected_bytes,
    }


def run_live(
    output_root: Path,
    *,
    confirmed_serial: str,
    profile: DwellProfile = SHORT_DWELL_PROFILE,
) -> dict[str, object]:
    """Capture and locally hand off one exact same-stream receive dwell."""

    require_live_confirmation(confirmed_serial)
    root = require_empty_output_root(output_root)
    tx_before = verify_tx2_muted(EXPECTED_URI, EXPECTED_SERIAL)
    spool = SQLiteLocalSpool(root / "capture-spool.sqlite3", root / "recordings")
    radio = V5RadioProvider().open()
    capture_wall_start_ns = time.monotonic_ns()
    try:
        completed = PlanCaptureEngine(CAPTURE_IDENTITY).execute(
            profile.plan, radio, SigMFRecordingWriter(), spool
        )
        capture_context_closed = False
    finally:
        try:
            radio.close()
            capture_context_closed = True
        finally:
            tx_after = verify_tx2_muted(EXPECTED_URI, EXPECTED_SERIAL)
    capture_wall_elapsed_ns = time.monotonic_ns() - capture_wall_start_ns

    local = RootedSigMFRecordingStore(root / "recordings")
    blobs = FileSystemBlobStore(root / "cas")
    catalog = InMemoryRecordingCatalog()
    publication = PublicationReconciler(
        spool,
        RecordingPublisherAdapter(local, blobs, catalog),
        local,
    ).reconcile()
    if (publication.published, publication.cleaned, publication.deferred) != (1, 1, 0):
        raise V5DwellE2EError("local dwell publication did not complete exactly once")
    published = catalog.get(str(completed.recording_id))
    if published is None:
        raise V5DwellE2EError("published dwell is absent from the in-memory catalog")
    if len(completed.manifest.segments) != 1:
        raise V5DwellE2EError("dwell manifest does not contain exactly one segment")
    segment = completed.manifest.segments[0]
    with SigMFRecordingObjectReader(blobs).open(
        published.recording_object
    ) as recording_view:
        if recording_view.manifest != completed.manifest:
            raise V5DwellE2EError("CAS dwell manifest differs from capture manifest")
        continuity = recording_view.continuity(segment.segment_id)
        if continuity is None:
            raise V5DwellE2EError("CAS dwell lacks V5 continuity metadata")
        frame_accounting = sustained_continuity_evidence(
            segment, continuity, expected_refill_count=profile.refill_count
        )
    integrity = object_integrity_evidence(blobs, published.recording_object, segment)

    restarted_spool = SQLiteLocalSpool(
        root / "capture-spool.sqlite3", root / "recordings"
    )
    restart_prevents_recapture = restarted_spool.has_durable_recording(
        profile.plan.plan_id
    )
    if not restart_prevents_recapture or restarted_spool.pending_publication():
        raise V5DwellE2EError("reopened dwell spool lost durable handoff state")

    metadata_span_value = frame_accounting["metadata_span_ns"]
    if not isinstance(metadata_span_value, int) or metadata_span_value <= 0:
        raise V5DwellE2EError("dwell metadata span is invalid")
    metadata_span_ns = metadata_span_value
    data_bytes = published.recording_object.data_object.byte_count
    actual_rate = segment.actual_sample_rate_hz
    required_payload_bytes_per_s = actual_rate * BYTES_PER_PAIRED_SAMPLE
    timing = {
        "capture_wall_elapsed_ns": capture_wall_elapsed_ns,
        "metadata_span_ns": metadata_span_ns,
        "rf_sample_duration_ns": round(segment.sample_count / actual_rate * 1e9),
        "capture_wall_payload_bytes_per_s": data_bytes
        / (capture_wall_elapsed_ns / 1e9),
        "metadata_span_payload_bytes_per_s": data_bytes / (metadata_span_ns / 1e9),
        "required_payload_bytes_per_s": required_payload_bytes_per_s,
    }
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "pass",
        "mode": "live-passive-rx-only-sustained-dwell",
        "radio_uri": EXPECTED_URI,
        "profile": profile.name,
        "plan_id": str(profile.plan.plan_id),
        "plan_digest": str(profile.digest),
        "recording_id": str(completed.recording_id),
        "activity_kind": completed.manifest.activities[0].kind.value,
        "segment": {
            "segment_id": str(segment.segment_id),
            "actual_center_frequency_hz": segment.actual_center_frequency_hz,
            "actual_sample_rate_hz": actual_rate,
            "actual_bandwidth_hz": segment.actual_bandwidth_hz,
            "sample_count_per_receiver": segment.sample_count,
            "shape": list(segment.shape),
        },
        "tx_evidence": {"before_capture": tx_before, "after_capture": tx_after},
        "capture_context": {"radio_close_completed": capture_context_closed},
        "frame_accounting": frame_accounting,
        "object_integrity": integrity,
        "timing": timing,
        "publication": {
            "published": publication.published,
            "cleaned": publication.cleaned,
            "deferred": publication.deferred,
            "restart_prevents_recapture": restart_prevents_recapture,
            "pending_after_restart": 0,
            "blob_backend": "local_filesystem_cas",
            "catalog_backend": "in_memory",
            "external_write_attempted": False,
        },
        "truth": {
            "kind": "passive-no-lnb-baseline",
            "known_signal_present": None,
            "scientific_detection_claim": False,
        },
    }
    (root / "dwell-e2e-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required live-radio arm")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--confirm-radio-serial", required=True)
    parser.add_argument(
        "--profile",
        choices=tuple(DWELL_PROFILES),
        default=SHORT_DWELL_PROFILE.name,
        help="named immutable dwell plan",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        raise V5DwellE2EError("--live is required; use pytest for fake metadata")
    report = run_live(
        arguments.output_root,
        confirmed_serial=arguments.confirm_radio_serial,
        profile=DWELL_PROFILES[arguments.profile],
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
