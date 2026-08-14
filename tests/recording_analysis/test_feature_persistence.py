from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.analysis.recording import (
    DurableFeatureSetRepository,
    FeatureSetIntegrityError,
    MalformedFeatureSetError,
    decode_feature_set,
    encode_feature_set,
)
from leo_flow.analysis.recording.codec import MAX_FEATURE_SET_BYTES
from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalog,
    FeatureSetCatalogProjection,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import (
    Covariance,
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
    MethodScore,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore


def _object(label: str, *, format_id: str = "fixture-v1") -> ObjectRef:
    payload = label.encode()
    return ObjectRef(
        Digest.sha256(payload),
        len(payload),
        "application/octet-stream",
        format_id,
        f"memory://{label}",
    )


def _fixture() -> tuple[RecordingAnalysisRequest, FeatureSetBundle]:
    recording_id = RecordingId("rec_feature_authority")
    recording = RecordingObjectRef(
        recording_id,
        _object("recording-data"),
        _object("recording-metadata"),
        Digest.sha256(b"manifest"),
    )
    algorithm = ArtifactRef("algorithm", Digest.sha256(b"algorithm"))
    config = ArtifactRef("config", Digest.sha256(b"config"))
    dependency = ArtifactRef("dependency", Digest.sha256(b"dependency"))
    request = RecordingAnalysisRequest(
        SchemaRef(RecordingAnalysisRequest.SCHEMA_ID),
        recording_id,
        recording,
        algorithm,
        config,
        (dependency,),
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )
    observation = FeatureObservation(
        FeatureId("feature_authority"),
        recording_id,
        SegmentId("seg_authority"),
        "detector",
        "1.0",
        0,
        16,
        16,
        UtcNs(100),
        "tone",
        0.75,
        "probability",
        receiver_chain_id=ReceiverChainId("rx_authority"),
        frequency_hz=1_825_000_000.0,
        covariance=Covariance(("frequency",), ("Hz2",), ((4.0,),)),
        uncertainty=(("label", "estimated"),),
        diagnostics=(("bins", (1, 2, 3)),),
    )
    bundle = FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        FeatureSetId("fset_authority"),
        AnalysisRunId("arun_authority"),
        recording_id,
        recording.identity_digest(),
        Provenance(
            "feature-producer",
            "1.0",
            "commit",
            Digest.sha256(b"environment"),
            config.digest,
            (recording.identity_digest(),),
            (algorithm.digest, dependency.digest),
            UtcNs(100),
            UtcNs(101),
            "test-host",
        ),
        (observation,),
        (
            MethodScore(
                "detector",
                "1.0",
                SegmentId("seg_authority"),
                "rx_authority",
                0,
                16,
                0.75,
                "probability",
            ),
        ),
        diagnostic_bundle_ref=_object("diagnostics", format_id="diagnostics-v1"),
        warnings=("descriptive",),
        reason_codes=("fixture",),
    )
    return request, bundle


class _Catalog(FeatureSetCatalog):
    def __init__(self) -> None:
        self.entry: CatalogedFeatureSet | None = None
        self.recording_ref: RecordingObjectRef | None = None
        self.calls = 0

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        self.calls += 1
        self.recording_ref = recording_ref
        entry = CatalogedFeatureSet(projection, bundle_ref)
        if self.entry is not None and self.entry != entry:
            raise RuntimeError("conflict")
        self.entry = entry
        return entry.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        return self.entry if self.entry is not None and self.entry.ref == ref else None


def test_codec_round_trips_all_feature_shapes() -> None:
    _, bundle = _fixture()
    payload = encode_feature_set(bundle)
    assert decode_feature_set(payload) == bundle
    assert encode_feature_set(decode_feature_set(payload)) == payload


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"x":1,"x":2}', "duplicate"),
        (b" " + encode_feature_set(_fixture()[1]), "canonical"),
        (b"[]", "root"),
        (b"0" * (MAX_FEATURE_SET_BYTES + 1), "size"),
    ],
)
def test_codec_rejects_ambiguous_noncanonical_or_oversized_bytes(
    payload: bytes, message: str
) -> None:
    with pytest.raises(MalformedFeatureSetError, match=message):
        decode_feature_set(payload)


def test_codec_rejects_unknown_fields() -> None:
    _, bundle = _fixture()
    document = json.loads(encode_feature_set(bundle))
    document["invented"] = True
    with pytest.raises(MalformedFeatureSetError, match="fields"):
        decode_feature_set(canonical_json_bytes(document))


def test_repository_publishes_one_blob_and_reads_exact_bundle(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableFeatureSetRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )

    ref = repository.publish(request, bundle, idempotency_key="feature:one")
    with repository.open(ref) as view:
        assert view.ref == ref
        assert view.bundle() == bundle
    assert catalog.recording_ref == request.recording_object_ref
    assert catalog.calls == 1
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


def test_validation_precedes_blob_or_catalog_publication(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableFeatureSetRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    invalid = replace(bundle, input_recording_identity_digest=Digest.sha256(b"wrong"))

    with pytest.raises(FeatureSetIntegrityError, match="recording identity"):
        repository.publish(request, invalid, idempotency_key="feature:invalid")
    assert catalog.calls == 0
    assert not tuple((tmp_path / "cas" / "sha256").glob("*/*"))


def test_reader_rejects_projection_disagreement(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableFeatureSetRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = repository.publish(request, bundle, idempotency_key="feature:projection")
    assert catalog.entry is not None
    catalog.entry = replace(
        catalog.entry,
        projection=replace(catalog.entry.projection, observation_count=99),
    )

    with (
        pytest.raises(FeatureSetIntegrityError, match="projection"),
        repository.open(ref),
    ):
        pass
