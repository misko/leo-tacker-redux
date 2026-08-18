# ruff: noqa: F401,F811 -- imported fixture is registered for this module.
from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_adaptive_response_codec import (
    MalformedStarlinkAdaptiveResponseError,
    decode_starlink_adaptive_response,
    encode_starlink_adaptive_response,
)
from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
    CatalogedStarlinkAdaptiveResponseV0_1,
    DurableRecordingStarlinkAdaptiveResponseQueryV0_1,
    DurableStarlinkAdaptiveResponseStoreV0_1,
    StarlinkAdaptiveResponseConflictError,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseQueryV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .test_starlink_adaptive_response import adaptive_response_result


class _Catalog:
    def __init__(self) -> None:
        self.item = None
        self.key = None

    def publish_starlink_adaptive_response(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):
        del recording_ref
        candidate = CatalogedStarlinkAdaptiveResponseV0_1(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        elif self.item != candidate or self.key != idempotency_key:
            raise StarlinkAdaptiveResponseConflictError("conflicting replay")
        return self.item.ref

    def get_starlink_adaptive_response(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_adaptive_response(self, recording_id):
        return (
            self.item.ref
            if self.item is not None and self.item.ref.recording_id == recording_id
            else None
        )


def test_codec_is_canonical_and_rejects_trailing_bytes(
    adaptive_response_result,
) -> None:
    _view, _request, bundle = adaptive_response_result
    payload = encode_starlink_adaptive_response(bundle)
    assert decode_starlink_adaptive_response(payload) == bundle
    assert (
        encode_starlink_adaptive_response(decode_starlink_adaptive_response(payload))
        == payload
    )
    with pytest.raises(MalformedStarlinkAdaptiveResponseError):
        decode_starlink_adaptive_response(payload + b"\n")


def test_cas_replay_filters_lnb_and_preserves_extrema(
    adaptive_response_result, tmp_path
) -> None:
    _view, request, bundle = adaptive_response_result
    catalog = _Catalog()
    store = DurableStarlinkAdaptiveResponseStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    first = store.publish(request, bundle, idempotency_key="adaptive:one")
    assert store.publish(request, bundle, idempotency_key="adaptive:one") == first
    with pytest.raises(StarlinkAdaptiveResponseConflictError):
        catalog.publish_starlink_adaptive_response(
            replace(
                catalog.item.projection,
                request_digest=Digest.sha256(b"conflict"),
            ),
            first.bundle_ref,
            request.recording_object_ref,
            idempotency_key="adaptive:one",
        )
    target = bundle.streams[0]
    result = DurableRecordingStarlinkAdaptiveResponseQueryV0_1(
        store, catalog
    ).recording_starlink_adaptive_response(
        StarlinkAdaptiveResponseQueryV0_1(
            bundle.recording_id,
            lnb_ids=(target.lnb_id,),
            maximum_points=3,
        )
    )
    assert len(result.streams) == 1
    assert result.streams[0].lnb_id == target.lnb_id
    assert len(result.streams[0].points) == 3
    assert result.truncated
    all_points = target.points
    shown = result.streams[0].points
    assert max(item.qin.score for item in shown) == max(
        item.qin.score for item in all_points
    )
    assert max(abs(item.qin_minus_max_surrogate) for item in shown) == max(
        abs(item.qin_minus_max_surrogate) for item in all_points
    )
