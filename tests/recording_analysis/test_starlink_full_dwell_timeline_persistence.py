from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    CatalogedFullDwellTimelineV0_1,
    DurableFullDwellTimelineStoreV0_1,
    FullDwellTimelineConflictError,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .test_starlink_full_dwell_timeline_product import _case


class _Catalog:
    def __init__(self) -> None:
        self.item = None
        self.key = None

    def publish_full_dwell_timeline(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):
        del recording_ref
        candidate = CatalogedFullDwellTimelineV0_1(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        elif self.item != candidate or self.key != idempotency_key:
            raise FullDwellTimelineConflictError("conflicting immutable replay")
        return self.item.ref

    def get_full_dwell_timeline(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None


def test_cas_first_timeline_is_immutable_and_exactly_replayable(tmp_path) -> None:
    _view, request, bundle = _case(19)
    catalog = _Catalog()
    store = DurableFullDwellTimelineStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    first = store.publish(request, bundle, idempotency_key="timeline:one")
    assert store.publish(request, bundle, idempotency_key="timeline:one") == first
    with store.open(first) as replay:
        assert replay == bundle
    with pytest.raises(FullDwellTimelineConflictError):
        catalog.publish_full_dwell_timeline(
            replace(
                catalog.item.projection,
                window_count=catalog.item.projection.window_count + 1,
            ),
            first.bundle_ref,
            request.recording_object_ref,
            idempotency_key="timeline:one",
        )
