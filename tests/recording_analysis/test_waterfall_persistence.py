from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from leo_flow.analysis.recording import (
    BoundedWaterfallAnalyzerV0_1,
    DurableWaterfallRepositoryV0_1,
    WaterfallConfigV0_1,
    WaterfallIntegrityError,
    waterfall_algorithm_ref_v0_1,
    waterfall_config_ref_v0_1,
)
from leo_flow.analysis.recording.waterfall_persistence import (
    CatalogedWaterfallV0_1,
    WaterfallCatalogProjectionV0_1,
    WaterfallCatalogV0_1,
)
from leo_flow.contracts.core import Digest, SchemaRef
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
    WaterfallProductRefV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .fakes import SegmentFixture, execution_context, make_view


def _fixture():
    data = struct.pack("<128h", *range(128))
    view, recording_ref = make_view(SegmentFixture(data, 16_000))
    config = WaterfallConfigV0_1(fft_window_samples=16, frequency_bins=8)
    request = WaterfallAnalysisRequestV0_1(
        SchemaRef(WaterfallAnalysisRequestV0_1.SCHEMA_ID),
        recording_ref.recording_id,
        recording_ref,
        waterfall_algorithm_ref_v0_1(),
        waterfall_config_ref_v0_1(config),
        (),
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
    )
    bundle = BoundedWaterfallAnalyzerV0_1(
        config, execution_context()
    ).analyze_waterfall(view, request)
    return request, bundle


class _Catalog(WaterfallCatalogV0_1):
    def __init__(self) -> None:
        self.entry: CatalogedWaterfallV0_1 | None = None
        self.recording_ref: RecordingObjectRef | None = None
        self.calls = 0

    def publish_waterfall(
        self,
        projection: WaterfallCatalogProjectionV0_1,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> WaterfallProductRefV0_1:
        self.calls += 1
        self.recording_ref = recording_ref
        entry = CatalogedWaterfallV0_1(projection, bundle_ref)
        if self.entry is not None and self.entry != entry:
            raise RuntimeError("conflict")
        self.entry = entry
        return entry.ref

    def get_waterfall(
        self, ref: WaterfallProductRefV0_1
    ) -> CatalogedWaterfallV0_1 | None:
        return self.entry if self.entry is not None and self.entry.ref == ref else None


def test_repository_publishes_one_blob_and_reads_exact_bundle(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableWaterfallRepositoryV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )

    ref = repository.publish(request, bundle, idempotency_key="waterfall:one")
    with repository.open(ref) as view:
        assert view.ref == ref
        assert view.bundle() == bundle
    assert catalog.recording_ref == request.recording_object_ref
    assert catalog.calls == 1
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


def test_validation_precedes_blob_and_catalog_publication(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableWaterfallRepositoryV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    invalid = replace(bundle, input_recording_identity_digest=Digest.sha256(b"wrong"))

    with pytest.raises(WaterfallIntegrityError, match="recording identity"):
        repository.publish(request, invalid, idempotency_key="waterfall:invalid")
    assert catalog.calls == 0
    assert not tuple((tmp_path / "cas" / "sha256").glob("*/*"))


def test_reader_rejects_projection_disagreement(tmp_path) -> None:
    request, bundle = _fixture()
    catalog = _Catalog()
    repository = DurableWaterfallRepositoryV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = repository.publish(request, bundle, idempotency_key="waterfall:projection")
    assert catalog.entry is not None
    catalog.entry = replace(
        catalog.entry,
        projection=replace(catalog.entry.projection, cell_count=99),
    )

    with (
        pytest.raises(WaterfallIntegrityError, match="projection"),
        repository.open(ref),
    ):
        pass
