from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationAnalyzerV0_3,
)
from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
    CatalogedStarlinkAcquiredConstellationV0_3,
    DurableRecordingStarlinkAcquiredConstellationQueryV0_3,
    DurableStarlinkAcquiredConstellationStoreV0_3,
    starlink_acquired_constellation_projection_v0_3,
)
from leo_flow.analysis.recording.starlink_acquired_constellation_recording_codec import (
    decode_starlink_acquired_constellation_recording,
    encode_starlink_acquired_constellation_recording,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    RecordingStarlinkAcquiredConstellationViewV0_3,
    StarlinkAcquiredConstellationOverallV0_3,
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
    StarlinkAcquiredConstellationRequestV0_3,
    StarlinkAcquiredConstellationStreamV0_3,
    StarlinkAcquiredConstellationViewMode,
    StarlinkAcquiredConstellationWindowV0_3,
)
from leo_flow.contracts.starlink_acquisition import V0_3
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore

from .fakes import execution_context
from .test_starlink_acquired_constellation_v0_3 import _products


def _object(value: bytes, locator: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(value),
        len(value),
        "application/octet-stream",
        "test-v0.1",
        locator,
    )


def _prepared():
    samples, suite, acquisition, config = _products()
    recording_ref = RecordingObjectRef(
        RecordingId("rec_acquired_qam"),
        _object(b"data", "cas:data"),
        _object(b"metadata", "cas:metadata"),
        Digest.sha256(b"manifest"),
    )
    identity = recording_ref.identity_digest()
    suite = replace(suite, recording_identity_digest=identity)
    acquisition = replace(acquisition, recording_identity_digest=identity)
    evidence = StarlinkAcquiredPilotConstellationAnalyzerV0_3(
        config,
        StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=len(samples)),
        execution_context(),
    ).analyze(samples, suite, acquisition, time_window_count=2)
    source = ArtifactRef(
        "slsuite_" + "1" * 32,
        Digest.sha256(b"suite"),
        SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle", V0_2),
    )
    windows = tuple(
        StarlinkAcquiredConstellationWindowV0_3(
            index,
            index * len(samples),
            (index + 1) * len(samples),
            UtcNs(1_000_000_000 + index * 10_000_000),
            UtcNs(1_010_000_000 + index * 10_000_000),
            acquisition,
            evidence,
        )
        for index in range(2)
    )
    overall = StarlinkAcquiredConstellationOverallV0_3(
        2,
        evidence.complete_frame_count * 2,
        evidence.hard_symbol_accuracy,
        evidence.rms_evm,
        evidence.model_snr_db,
        evidence.held_out_verify_score,
        evidence.verify_minus_control_margin,
        0,
    )
    stream = StarlinkAcquiredConstellationStreamV0_3(
        RadioId("radio_test"),
        evidence.segment_id,
        evidence.receiver_chain_id,
        evidence.edge,
        evidence.sample_rate_hz,
        len(samples) * 2,
        windows,
        overall,
    )
    request = StarlinkAcquiredConstellationRequestV0_3(
        SchemaRef(StarlinkAcquiredConstellationRequestV0_3.SCHEMA_ID, V0_3),
        recording_ref.recording_id,
        recording_ref,
        source,
        Digest.sha256(b"suite-request"),
        ((stream.radio_id, stream.segment_id, stream.receiver_chain_id, stream.edge),),
        2,
        SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
    )
    bundle = StarlinkAcquiredConstellationRecordingBundleV0_3(
        SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
        "slqam3rec_" + "2" * 32,
        request.recording_id,
        identity,
        source,
        request.source_suite_request_digest,
        request.digest,
        (stream,),
        (
            "candidate-evidence-not-calibrated-detection",
            "whole-revised-search-calibration-required",
            "published-edge-pilot-not-user-payload",
            "bounded-window-sampling-across-dwell",
        ),
        None,
    )
    return request, bundle


class _Catalog:
    def __init__(self) -> None:
        self.item: CatalogedStarlinkAcquiredConstellationV0_3 | None = None

    def publish_starlink_acquired_constellation(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):
        del recording_ref, idempotency_key
        incoming = CatalogedStarlinkAcquiredConstellationV0_3(projection, bundle_ref)
        if self.item is None:
            self.item = incoming
        return self.item.ref

    def get_starlink_acquired_constellation(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_acquired_constellation(self, recording_id):
        return (
            self.item.ref
            if self.item is not None
            and self.item.projection.recording_id == recording_id
            else None
        )


class _Lnb:
    def lnb_id_for_recording_receiver(self, recording_id, receiver_chain_id):
        del recording_id, receiver_chain_id
        return "lnb-authoritative"


def test_v0_3_recording_codec_is_canonical_and_fail_closed() -> None:
    _request, bundle = _prepared()
    payload = encode_starlink_acquired_constellation_recording(bundle)
    assert decode_starlink_acquired_constellation_recording(payload) == bundle
    with pytest.raises(ValueError):
        decode_starlink_acquired_constellation_recording(payload + b" ")
    with pytest.raises(ValueError, match="cannot count detections"):
        replace(bundle, calibrated_detection_count=1)


def test_durable_v0_3_query_supports_overall_windows_radio_and_lnb(tmp_path) -> None:
    request, bundle = _prepared()
    catalog = _Catalog()
    store = DurableStarlinkAcquiredConstellationStoreV0_3(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = store.publish(request, bundle, idempotency_key="qam-v0.3:test")
    assert store.publish(request, bundle, idempotency_key="qam-v0.3:test") == ref
    query = DurableRecordingStarlinkAcquiredConstellationQueryV0_3(
        store, catalog, _Lnb()
    )
    overall = query.recording_starlink_acquired_constellation(
        StarlinkAcquiredConstellationQueryV0_3(
            request.recording_id,
            radio_ids=(RadioId("radio_test"),),
            lnb_ids=("lnb-authoritative",),
        )
    )
    assert isinstance(overall, RecordingStarlinkAcquiredConstellationViewV0_3)
    assert len(overall.streams[0].windows) == 1
    assert overall.candidate_only and overall.calibration_required
    windows = query.recording_starlink_acquired_constellation(
        StarlinkAcquiredConstellationQueryV0_3(
            request.recording_id,
            mode=StarlinkAcquiredConstellationViewMode.WINDOWS,
            maximum_windows_per_stream=2,
            maximum_points_per_constellation=100,
        )
    )
    assert len(windows.streams[0].windows) == 2
    assert len(windows.streams[0].windows[0].display_points) == 100
    assert windows.streams[0].lnb_id == "lnb-authoritative"


def test_v0_3_projection_closes_request_streams_and_window_count() -> None:
    request, bundle = _prepared()
    projection = starlink_acquired_constellation_projection_v0_3(request, bundle)
    assert (
        projection.stream_count,
        projection.window_count,
        projection.point_count,
    ) == (1, 2, 4_800)
    assert projection.calibration_required is True
