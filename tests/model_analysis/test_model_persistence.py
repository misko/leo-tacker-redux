from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.analysis.model import (
    DurableModelSnapshotRepository,
    MalformedModelSnapshotError,
    ModelSnapshotIntegrityError,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
    decode_model_snapshot,
    encode_model_snapshot,
)
from leo_flow.analysis.model.codec import MAX_MODEL_SNAPSHOT_BYTES
from leo_flow.analysis.model.persistence import (
    CatalogedModelSnapshot,
    ModelSnapshotCatalogProjection,
)
from leo_flow.contracts.core import Digest, UtcNs, canonical_json_bytes
from leo_flow.contracts.model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore

from .fakes import (
    FakeEphemerisReader,
    FakeFeatureSetReader,
    FakeHardwareReader,
    dataset,
    execution_context,
    feature_set,
    hardware_snapshot,
    request,
)


def _fixture() -> tuple[ModelAnalysisRequest, ModelSnapshotBundle]:
    first = feature_set(1, (("rx_0", 10.0, 1.0),))
    second = feature_set(2, (("rx_0", 20.0, 1.0),))
    snapshot = dataset((first[0], second[0]))
    hardware = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    model_request = request(snapshot, config, (hardware[0],))
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        model_request,
        FakeFeatureSetReader((first, second)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hardware,)),
    )
    return model_request, bundle


class _Catalog:
    def __init__(self) -> None:
        self.entry: CatalogedModelSnapshot | None = None
        self.releases: list[tuple[str, ModelRelease]] = []
        self.calls = 0

    def publish(
        self,
        projection: ModelSnapshotCatalogProjection,
        bundle_ref: ObjectRef,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef:
        del request, bundle, idempotency_key
        self.calls += 1
        entry = CatalogedModelSnapshot(projection, bundle_ref)
        if self.entry is not None and self.entry != entry:
            raise RuntimeError("conflict")
        self.entry = entry
        return entry.ref

    def get(self, ref: ModelSnapshotRef) -> CatalogedModelSnapshot | None:
        return self.entry if self.entry is not None and self.entry.ref == ref else None

    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease:
        release = ModelRelease(alias, model_ref, approval)
        self.releases.append((idempotency_key, release))
        return release

    def get_release(self, alias: str) -> ModelRelease | None:
        matches = [release for _, release in self.releases if release.alias == alias]
        return matches[-1] if matches else None


def test_codec_round_trips_complete_model_bundle() -> None:
    _, bundle = _fixture()
    payload = encode_model_snapshot(bundle)
    assert decode_model_snapshot(payload) == bundle
    assert encode_model_snapshot(decode_model_snapshot(payload)) == payload


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"x":1,"x":2}', "duplicate"),
        (b" " + encode_model_snapshot(_fixture()[1]), "canonical"),
        (b"[]", "root"),
        (b"0" * (MAX_MODEL_SNAPSHOT_BYTES + 1), "size"),
    ],
)
def test_codec_rejects_ambiguous_noncanonical_or_oversized_bytes(
    payload: bytes, message: str
) -> None:
    with pytest.raises(MalformedModelSnapshotError, match=message):
        decode_model_snapshot(payload)


def test_codec_rejects_unknown_fields() -> None:
    _, bundle = _fixture()
    document = json.loads(encode_model_snapshot(bundle))
    document["invented"] = True
    with pytest.raises(MalformedModelSnapshotError, match="fields"):
        decode_model_snapshot(canonical_json_bytes(document))


def test_repository_publishes_one_blob_and_reads_exact_bundle(tmp_path) -> None:
    model_request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableModelSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )

    ref = repository.publish(model_request, bundle, idempotency_key="model:one")
    with repository.open(ref) as view:
        assert view.ref == ref
        assert view.bundle() == bundle
    assert catalog.calls == 1
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


def test_validation_precedes_blob_publication(tmp_path) -> None:
    model_request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableModelSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    invalid = replace(bundle, dataset_membership_digest=Digest.sha256(b"wrong"))

    with pytest.raises(ModelSnapshotIntegrityError, match="dataset membership"):
        repository.publish(model_request, invalid, idempotency_key="model:invalid")
    assert catalog.calls == 0
    assert not tuple((tmp_path / "cas" / "sha256").glob("*/*"))


def test_reader_rejects_projection_disagreement(tmp_path) -> None:
    model_request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableModelSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = repository.publish(model_request, bundle, idempotency_key="model:projection")
    assert catalog.entry is not None
    catalog.entry = replace(
        catalog.entry,
        projection=replace(catalog.entry.projection, parameter_count=99),
    )
    with (
        pytest.raises(ModelSnapshotIntegrityError, match="projection"),
        repository.open(ref),
    ):
        pass


def test_release_is_explicit_and_alias_can_advance(tmp_path) -> None:
    model_request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableModelSnapshotRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    # The fake release catalog is sufficient here; durable CAS behavior is above.
    first_ref = ModelSnapshotRef(
        bundle.model_snapshot_id,
        bundle.model_run_id,
        ObjectRef(
            Digest.sha256(b"first"),
            5,
            "application/json",
            "model-snapshot-bundle-v0.1",
            "memory://first",
        ),
    )
    first = repository.release(
        first_ref,
        "current",
        ModelApproval("reviewer", UtcNs(10), "first approved"),
        idempotency_key="release:first",
    )
    second = repository.release(
        replace(
            first_ref,
            bundle_ref=replace(first_ref.bundle_ref, locator="memory://moved"),
        ),
        "current",
        ModelApproval("reviewer", UtcNs(20), "relocation approved"),
        idempotency_key="release:second",
    )
    assert first != second
    assert repository.get_release("current") == second
    del model_request
