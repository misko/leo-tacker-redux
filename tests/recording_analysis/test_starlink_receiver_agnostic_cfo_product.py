from __future__ import annotations

import pytest

from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_codec import (
    MalformedReceiverAgnosticCfoQamError,
    decode_receiver_agnostic_cfo_qam_bundle,
    decode_receiver_agnostic_cfo_qam_request,
    encode_receiver_agnostic_cfo_qam_bundle,
    encode_receiver_agnostic_cfo_qam_request,
)
from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
    CatalogedReceiverAgnosticCfoQamV0_6,
    DurableReceiverAgnosticCfoQamStoreV0_6,
    DurableRecordingReceiverAgnosticCfoQamQueryV0_6,
)
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamQueryV0_6,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .receiver_agnostic_cfo_product_fixtures import product_pair


class _Catalog:
    item = None
    key = None

    def publish_receiver_agnostic_cfo_qam(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):  # type: ignore[no-untyped-def]
        del recording_ref
        candidate = CatalogedReceiverAgnosticCfoQamV0_6(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        assert self.item == candidate and self.key == idempotency_key
        return self.item.ref

    def get_receiver_agnostic_cfo_qam(self, ref):  # type: ignore[no-untyped-def]
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_receiver_agnostic_cfo_qam(self, recording_id):  # type: ignore[no-untyped-def]
        return (
            self.item.ref
            if self.item is not None and self.item.ref.recording_id == recording_id
            else None
        )


def test_canonical_codecs_preserve_exact_bounded_product() -> None:
    request, bundle = product_pair()
    encoded_request = encode_receiver_agnostic_cfo_qam_request(request)
    encoded_bundle = encode_receiver_agnostic_cfo_qam_bundle(bundle)
    assert decode_receiver_agnostic_cfo_qam_request(encoded_request) == request
    assert decode_receiver_agnostic_cfo_qam_bundle(encoded_bundle) == bundle
    with pytest.raises(MalformedReceiverAgnosticCfoQamError):
        decode_receiver_agnostic_cfo_qam_bundle(encoded_bundle + b"\n")


def test_cas_first_publish_and_bounded_query_preserve_declared_domain(tmp_path) -> None:
    request, bundle = product_pair()
    catalog = _Catalog()
    store = DurableReceiverAgnosticCfoQamStoreV0_6(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = store.publish(request, bundle, idempotency_key="cfo-qam:test")
    assert store.publish(request, bundle, idempotency_key="cfo-qam:test") == ref
    view = DurableRecordingReceiverAgnosticCfoQamQueryV0_6(
        store, catalog
    ).recording_receiver_agnostic_cfo_qam(
        ReceiverAgnosticCfoQamQueryV0_6(bundle.recording_id)
    )
    assert view.total_window_count == 1
    assert (view.windows[0].cfo_min_hz, view.windows[0].cfo_max_hz) == (
        -700_000.0,
        700_000.0,
    )
    assert tuple(item.pattern_index for item in view.windows[0].patterns) == (0, 1)
    assert view.candidates_only and view.calibrated_detection_count is None
