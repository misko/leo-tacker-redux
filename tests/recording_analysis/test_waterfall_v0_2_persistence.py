from __future__ import annotations

import io
from dataclasses import replace

from leo_flow.analysis.recording.waterfall_v0_2_codec import (
    WATERFALL_V0_2_FORMAT_ID,
    WATERFALL_V0_2_MEDIA_TYPE,
    encode_waterfall_bundle_v0_2,
)
from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    DurableWaterfallReaderV0_2,
    waterfall_projection_v0_2,
)
from leo_flow.contracts.core import ArtifactRef, Digest, Provenance, SchemaRef, UtcNs
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
    WaterfallProductRefV0_2,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.recording_analysis.test_waterfall_doppler_pipeline import _bundle


def test_projection_and_reader_close_over_exact_v0_2_bytes(tmp_path) -> None:
    original = _bundle()
    recording_ref = RecordingObjectRef(
        original.recording_id,
        ObjectRef(
            Digest.sha256(b"data"), 4, "application/octet-stream", "ci16", "cas:data"
        ),
        ObjectRef(
            Digest.sha256(b"metadata"), 4, "application/json", "json", "cas:metadata"
        ),
        Digest.sha256(b"manifest"),
    )
    recording_digest = recording_ref.identity_digest()
    algorithm = ArtifactRef("waterfall-v0.2", Digest.sha256(b"algorithm"))
    config = ArtifactRef("waterfall-config-v0.2", Digest.sha256(b"config"))
    bundle = replace(
        original,
        input_recording_identity_digest=recording_digest,
        provenance=Provenance(
            "test",
            "0.2",
            "commit",
            Digest.sha256(b"environment"),
            config.digest,
            (recording_digest,),
            (algorithm.digest,),
            UtcNs(1),
            UtcNs(2),
            "test-host",
        ),
    )
    request = WaterfallAnalysisRequestV0_2(
        SchemaRef(WaterfallAnalysisRequestV0_2.SCHEMA_ID, V0_2),
        bundle.recording_id,
        recording_ref,
        algorithm,
        config,
        (),
        SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
    )
    projection = waterfall_projection_v0_2(request, bundle)
    assert projection.tile_count == 1
    assert projection.pixel_count == 192

    payload = encode_waterfall_bundle_v0_2(bundle)
    store = FileSystemBlobStore(tmp_path / "cas")
    blob = store.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type=WATERFALL_V0_2_MEDIA_TYPE,
        format_id=WATERFALL_V0_2_FORMAT_ID,
        idempotency_key="waterfall-v0.2",
    )
    ref = WaterfallProductRefV0_2(
        bundle.product_id, bundle.analysis_run_id, bundle.recording_id, blob
    )

    class _Lookup:
        def get_waterfall_v0_2(self, product_id):
            return ref if product_id == str(ref.product_id) else None

    with DurableWaterfallReaderV0_2(store, _Lookup()).open(str(ref.product_id)) as view:
        assert view.ref == ref
        assert view.bundle == bundle
