from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
    CatalogedStarlinkPilotConstellationV0_1,
    DurableRecordingStarlinkPilotConstellationQueryV0_1,
    DurableStarlinkPilotConstellationStoreV0_1,
    StarlinkPilotConstellationConflictError,
    starlink_pilot_constellation_projection_v0_1,
)
from leo_flow.analysis.recording.starlink_pilot_constellation_recording_codec import (
    decode_starlink_pilot_constellation_recording,
    encode_starlink_pilot_constellation_recording,
)
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationCatalogProjectionV0_1,
    StarlinkPilotConstellationProductRefV0_1,
    StarlinkPilotConstellationQueryV0_1,
    StarlinkPilotConstellationRecordingBundleV0_1,
    StarlinkPilotConstellationRequestV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore

from .fakes import execution_context
from .test_starlink_pilot_constellation import _fixture


def _object(label: bytes, locator: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(label),
        len(label),
        "application/octet-stream",
        "test-object-v0.1",
        locator,
    )


def _prepared():
    samples, original_suite = _fixture()
    recording_id = RecordingId("rec_qam_oracle")
    recording_ref = RecordingObjectRef(
        recording_id,
        _object(b"data", "cas:data"),
        _object(b"metadata", "cas:metadata"),
        Digest.sha256(b"manifest"),
    )
    suite = replace(
        original_suite, recording_identity_digest=recording_ref.identity_digest()
    )
    evidence = StarlinkPilotConstellationAnalyzerV0_1(
        StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=20_000),
        execution_context(),
    ).analyze(samples, suite)
    source_ref = ArtifactRef(
        "slsuite_" + "1" * 32,
        Digest.sha256(b"source-suite-bundle"),
        SchemaRef("org.leo-flow.starlink-detector-suite-recording-bundle", V0_2),
    )
    keys = ((evidence.segment_id, evidence.receiver_chain_id, evidence.edge),)
    request = StarlinkPilotConstellationRequestV0_1(
        SchemaRef(StarlinkPilotConstellationRequestV0_1.SCHEMA_ID),
        recording_id,
        recording_ref,
        source_ref,
        Digest.sha256(b"source-suite-request"),
        keys,
        SchemaRef(StarlinkPilotConstellationRecordingBundleV0_1.SCHEMA_ID),
    )
    bundle = StarlinkPilotConstellationRecordingBundleV0_1(
        SchemaRef(StarlinkPilotConstellationRecordingBundleV0_1.SCHEMA_ID),
        "slqamrec_" + "2" * 32,
        recording_id,
        recording_ref.identity_digest(),
        source_ref,
        request.source_suite_request_digest,
        request.digest,
        (evidence,),
        (
            "candidate-evidence-not-calibrated-detection",
            "published-edge-pilot-not-user-payload",
        ),
        None,
    )
    return request, bundle


class _Catalog:
    def __init__(self) -> None:
        self.item: CatalogedStarlinkPilotConstellationV0_1 | None = None
        self.key: str | None = None

    def publish_starlink_pilot_constellation(
        self,
        projection: StarlinkPilotConstellationCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> StarlinkPilotConstellationProductRefV0_1:
        candidate = CatalogedStarlinkPilotConstellationV0_1(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        elif self.item != candidate or self.key != idempotency_key:
            raise StarlinkPilotConstellationConflictError("conflict")
        return self.item.ref

    def get_starlink_pilot_constellation(
        self, ref: StarlinkPilotConstellationProductRefV0_1
    ) -> CatalogedStarlinkPilotConstellationV0_1 | None:
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_pilot_constellation(
        self, recording_id: RecordingId
    ) -> StarlinkPilotConstellationProductRefV0_1 | None:
        return (
            self.item.ref
            if self.item is not None
            and self.item.projection.recording_id == recording_id
            else None
        )


def test_recording_codec_projection_durable_replay_and_bounded_query(
    tmp_path: Path,
) -> None:
    request, bundle = _prepared()
    payload = encode_starlink_pilot_constellation_recording(bundle)
    assert decode_starlink_pilot_constellation_recording(payload) == bundle
    projection = starlink_pilot_constellation_projection_v0_1(request, bundle)
    assert projection.stream_count == 1 and projection.point_count == 2_400
    catalog = _Catalog()
    store = DurableStarlinkPilotConstellationStoreV0_1(
        FileSystemBlobStore(tmp_path), catalog
    )
    ref = store.publish(request, bundle, idempotency_key="qam:test")
    assert store.publish(request, bundle, idempotency_key="qam:test") == ref
    with store.open(ref) as opened:
        assert opened == bundle
    view = DurableRecordingStarlinkPilotConstellationQueryV0_1(
        store, catalog
    ).recording_starlink_pilot_constellation(
        StarlinkPilotConstellationQueryV0_1(
            request.recording_id, maximum_points_per_stream=100
        )
    )
    assert len(view.streams[0].display_points) == 100
    assert view.streams[0].evidence_digest == bundle.streams[0].digest
    assert view.streams[0].original_point_count == 2_400
    assert view.truncated is True


def test_projection_rejects_source_or_stream_mismatch() -> None:
    request, bundle = _prepared()
    with pytest.raises(Exception, match="request and bundle"):
        starlink_pilot_constellation_projection_v0_1(
            request,
            replace(bundle, source_suite_request_digest=Digest.sha256(b"other")),
        )
