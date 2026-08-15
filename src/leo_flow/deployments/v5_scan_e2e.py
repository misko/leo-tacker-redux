"""Operator-owned live E2E harness for the exact V5 scan plan.

This module deliberately composes capture and analysis only as a validation
harness.  The production scan deployment remains capture-only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from leo_flow.analysis.recording import (
    AnalysisExecutionContext,
    DurableFeatureSetRepository,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.capture.engine import PlanCaptureEngine
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.continuity import SegmentContinuity
from leo_flow.contracts.core import ArtifactRef, Digest, FeatureSetId, SchemaRef, UtcNs
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.deployments.v5_canary import V5RadioProvider
from leo_flow.deployments.v5_scan import CAPTURE_IDENTITY, PLAN_ID, SCAN_PLAN
from leo_flow.jobs import InMemoryJobLeaseRepository, JobState
from leo_flow.services.recording_analysis import (
    FencedRecordingAnalysisWorker,
    PreparedRecordingAnalysis,
    RecordingAnalysisJobPreparer,
)
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from leo_flow.storage.catalog import InMemoryRecordingCatalog, RecordingPublisherAdapter
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)

EXPECTED_SERIAL = "104000b29905000e17000800065934759d"
EXPECTED_URI = "ip:192.168.1.15"
REPORT_SCHEMA = "org.leo-flow.v5-scan-e2e-report/v2"


class V5ScanE2EError(RuntimeError):
    """The live scan rehearsal failed a safety or completeness gate."""


class _FeatureCatalog:
    def __init__(self) -> None:
        self._entries: dict[FeatureSetId, CatalogedFeatureSet] = {}

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: Any,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        del recording_ref, idempotency_key
        entry = CatalogedFeatureSet(projection, bundle_ref)
        key = entry.ref.feature_set_id
        existing = self._entries.get(key)
        if existing is not None and existing != entry:
            raise V5ScanE2EError("feature-set identity conflict")
        self._entries[key] = entry
        return entry.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        entry = self._entries.get(ref.feature_set_id)
        return entry if entry is not None and entry.ref == ref else None


class _FeatureCommitter:
    def __init__(
        self,
        jobs: InMemoryJobLeaseRepository,
        features: DurableFeatureSetRepository,
    ) -> None:
        self._jobs = jobs
        self._features = features
        self.ref: FeatureSetRef | None = None

    def commit(self, lease: Any, prepared: PreparedRecordingAnalysis) -> ArtifactRef:
        self.ref = self._features.publish(
            prepared.request,
            prepared.bundle,
            idempotency_key=f"recording-analysis:{lease.job_id}",
        )
        result = ArtifactRef(
            str(self.ref.feature_set_id),
            self.ref.bundle_ref.digest,
            prepared.bundle.schema,
        )
        self._jobs.complete(
            lease.job_id, lease.lease_token, lease.lease_generation, result
        )
        return result


def _require_empty_output_root(path: Path) -> Path:
    if not path.is_absolute():
        raise V5ScanE2EError("output root must be absolute")
    root = path.resolve()
    if root.exists() and not root.is_dir():
        raise V5ScanE2EError("output root must be a directory")
    if root.exists() and any(root.iterdir()):
        raise V5ScanE2EError("output root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _require_live_confirmation(value: str) -> None:
    if value != EXPECTED_SERIAL:
        raise V5ScanE2EError(
            "live E2E requires the exact expected radio serial as confirmation"
        )


def _verify_tx2_muted() -> dict[str, object]:
    """Read only the selected context and refuse a non-muted DDS path."""

    iio = importlib.import_module("iio")
    context = iio.Context(EXPECTED_URI)
    try:
        serial = str(context.attrs["hw_serial"])
        if serial != EXPECTED_SERIAL:
            raise V5ScanE2EError("TX mute check reached a different radio serial")
        phy = context.find_device("ad9361-phy")
        dds = context.find_device("cf-ad9361-dds-core-lpc")
        if phy is None or dds is None:
            raise V5ScanE2EError("TX mute check cannot find PHY or DDS")
        tx2_gain = None
        for channel in phy.channels:
            if channel.output and channel.id == "voltage1":
                tx2_gain = float(channel.attrs["hardwaregain"].value.split()[0])
                break
        if tx2_gain is None or tx2_gain > -80.0:
            raise V5ScanE2EError("TX2 hardware gain is not at the muted floor")
        scales: dict[str, float] = {}
        for channel in dds.channels:
            if channel.output and channel.id in {
                "altvoltage4",
                "altvoltage5",
                "altvoltage6",
                "altvoltage7",
            }:
                scales[channel.id] = float(channel.attrs["scale"].value)
        if set(scales) != {
            "altvoltage4",
            "altvoltage5",
            "altvoltage6",
            "altvoltage7",
        } or any(value != 0.0 for value in scales.values()):
            raise V5ScanE2EError("TX2 DDS scales are not all zero")
        return {
            "radio_serial": serial,
            "tx2_hardware_gain_db": tx2_gain,
            "tx2_dds_scales": scales,
            "read_only_check": True,
        }
    finally:
        destroy = getattr(context, "destroy", None)
        if callable(destroy):
            destroy()


def _frame_accounting(
    segment: SegmentManifest, evidence: SegmentContinuity
) -> dict[str, object]:
    """Summarize exactly what V5 metadata can prove for one stored segment."""

    stored_samples = sum(refill.sample_count for refill in evidence.refills)
    if stored_samples != segment.sample_count:
        raise V5ScanE2EError(
            f"segment {segment.segment_id} refill coverage differs from manifest"
        )
    return {
        "continuity_status": evidence.status.value,
        "refill_count": len(evidence.refills),
        "stored_sample_count": stored_samples,
        "gap_count": len(evidence.gaps),
        "missing_buffer_count": sum(gap.missing_buffer_count for gap in evidence.gaps),
        "missing_sample_count": sum(gap.missing_sample_count for gap in evidence.gaps),
        "buffer_sequences": [refill.buffer_sequence for refill in evidence.refills],
        "first_sample_sequences": [
            refill.first_sample_sequence for refill in evidence.refills
        ],
        "stream_ids": sorted({refill.stream_id for refill in evidence.refills}),
        "flags": sorted(
            {flag.value for refill in evidence.refills for flag in refill.flags}
        ),
    }


def _object_integrity(
    blobs: FileSystemBlobStore,
    published: RecordingObjectRef,
    segments: tuple[SegmentManifest, ...],
) -> dict[str, object]:
    """Re-hash both local CAS objects and reconcile their declared extents."""

    data = blobs.head(published.data_object)
    metadata = blobs.head(published.metadata_object)
    if not data.verified or not metadata.verified:
        raise V5ScanE2EError("local CAS object integrity is unverified")
    expected_data_bytes = sum(
        segment.shape[0] * segment.shape[1] * segment.shape[2] * 2
        for segment in segments
    )
    if published.data_object.byte_count != expected_data_bytes:
        raise V5ScanE2EError("paired IQ object extent differs from the manifest")
    return {
        "data": {
            "digest": str(published.data_object.digest),
            "byte_count": published.data_object.byte_count,
            "verified": data.verified,
        },
        "metadata": {
            "digest": str(published.metadata_object.digest),
            "byte_count": published.metadata_object.byte_count,
            "verified": metadata.verified,
        },
        "manifest_digest": str(published.manifest_digest),
        "expected_data_byte_count": expected_data_bytes,
    }


def run_live(output_root: Path, *, confirmed_serial: str) -> dict[str, object]:
    """Capture, publish, read, submit, and analyze one passive eight-tuning scan."""

    _require_live_confirmation(confirmed_serial)
    root = _require_empty_output_root(output_root)
    tx_evidence_before = _verify_tx2_muted()
    spool = SQLiteLocalSpool(root / "capture-spool.sqlite3", root / "recordings")
    radio = V5RadioProvider().open()
    try:
        completed = PlanCaptureEngine(CAPTURE_IDENTITY).execute(
            SCAN_PLAN, radio, SigMFRecordingWriter(), spool
        )
    finally:
        radio.close()
    tx_evidence_after = _verify_tx2_muted()

    local = RootedSigMFRecordingStore(root / "recordings")
    blobs = FileSystemBlobStore(root / "cas")
    recordings = InMemoryRecordingCatalog()
    publication = PublicationReconciler(
        spool,
        RecordingPublisherAdapter(local, blobs, recordings),
        local,
    ).reconcile()
    if publication.deferred or publication.published != 1:
        raise V5ScanE2EError("recording publication did not complete exactly once")
    published = recordings.get(str(completed.recording_id))
    if published is None:
        raise V5ScanE2EError("published recording is absent from the catalog")

    continuity: dict[str, str] = {}
    frame_accounting: dict[str, dict[str, object]] = {}
    with SigMFRecordingObjectReader(blobs).open(
        published.recording_object
    ) as recording_view:
        if recording_view.manifest != completed.manifest:
            raise V5ScanE2EError(
                "local CAS recording manifest differs from captured manifest"
            )
        for segment in completed.manifest.segments:
            evidence = recording_view.continuity(segment.segment_id)
            if evidence is None or not evidence.is_verified or evidence.gaps:
                raise V5ScanE2EError(
                    f"segment {segment.segment_id} lacks contiguous V5 evidence"
                )
            continuity[str(segment.segment_id)] = evidence.status.value
            frame_accounting[str(segment.segment_id)] = _frame_accounting(
                segment, evidence
            )
    object_integrity = _object_integrity(
        blobs,
        published.recording_object,
        completed.manifest.segments,
    )

    config = QualityPsdConfig(
        psd_window_samples=256,
        psd_stride_samples=262_144,
        clip_threshold_abs=32_760,
    )
    jobs = InMemoryJobLeaseRepository()
    submitted = RecordingAnalysisSubmissionService(jobs).submit(
        RecordingAnalysisSubmission(
            published,
            quality_psd_algorithm_ref(),
            quality_psd_config_ref(config),
            (),
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
        )
    )
    feature_catalog = _FeatureCatalog()
    features = DurableFeatureSetRepository(blobs, feature_catalog)
    committer = _FeatureCommitter(jobs, features)
    now = time.time_ns()
    worker = FencedRecordingAnalysisWorker(
        jobs,
        RecordingAnalysisJobPreparer(
            SigMFRecordingObjectReader(blobs),
            QualityPsdAnalyzer(
                config,
                AnalysisExecutionContext(
                    "v5-scan-e2e-quality-psd",
                    "0.1.0",
                    "working-tree",
                    Digest.sha256(b"leo-flow-v5-qualified-runtime"),
                    UtcNs(now),
                    UtcNs(now + 1),
                    "qualified-v5-container",
                ),
            ),
        ),
        committer,
        worker_id="v5-scan-e2e",
        lease_ttl_s=300,
    )
    if not worker.process_one_job():
        raise V5ScanE2EError("recording-analysis job was not claimed")
    if jobs.snapshot(submitted.job_id).state is not JobState.SUCCEEDED:
        raise V5ScanE2EError("recording-analysis job did not succeed")
    if committer.ref is None:
        raise V5ScanE2EError("feature set was not published")
    with features.open(committer.ref) as view:
        bundle = view.bundle()
    restarted_spool = SQLiteLocalSpool(
        root / "capture-spool.sqlite3", root / "recordings"
    )
    restart_prevents_recapture = restarted_spool.has_durable_recording(PLAN_ID)
    if not restart_prevents_recapture:
        raise V5ScanE2EError("reopened spool lost the durable plan admission gate")

    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "pass",
        "mode": "live-passive-rx-only",
        "radio_uri": EXPECTED_URI,
        "tx_evidence": {
            "before_capture": tx_evidence_before,
            "after_capture": tx_evidence_after,
        },
        "plan_id": str(PLAN_ID),
        "recording_id": str(completed.recording_id),
        "recording_identity_digest": str(published.recording_object.identity_digest()),
        "activity_kinds": [item.kind.value for item in completed.manifest.activities],
        "segment_count": len(completed.manifest.segments),
        "segment_centers_hz": [
            item.actual_center_frequency_hz for item in completed.manifest.segments
        ],
        "segment_sample_counts": [
            item.sample_count for item in completed.manifest.segments
        ],
        "continuity": continuity,
        "frame_accounting": {
            "scope": "within-segment; stream is recreated at every tuning",
            "inter_segment_gaps": "not_applicable_retune_boundaries",
            "segments": frame_accounting,
        },
        "object_integrity": object_integrity,
        "publication": {
            "published": publication.published,
            "cleaned": publication.cleaned,
            "deferred": publication.deferred,
            "restart_prevents_recapture": restart_prevents_recapture,
            "blob_backend": "local_filesystem_cas",
            "catalog_backend": "in_memory",
            "external_write_attempted": False,
        },
        "analysis": {
            "job_id": str(submitted.job_id),
            "feature_set_id": str(bundle.feature_set_id),
            "observation_count": len(bundle.observations),
            "method_score_count": len(bundle.method_scores),
            "warnings": list(bundle.warnings),
            "reason_codes": list(bundle.reason_codes),
        },
        "truth": {
            "kind": "passive-no-lnb-baseline",
            "known_signal_present": None,
            "scientific_detection_claim": False,
        },
    }
    (root / "e2e-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required live-radio arm")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--confirm-radio-serial", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        raise V5ScanE2EError("--live is required; use pytest for the fake-radio E2E")
    report = run_live(
        arguments.output_root,
        confirmed_serial=arguments.confirm_radio_serial,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
