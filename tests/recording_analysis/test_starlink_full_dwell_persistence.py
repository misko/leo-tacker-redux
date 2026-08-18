# ruff: noqa: F401,F811 -- imported fixture is registered for this module.
from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
    CatalogedStarlinkFullDwellV0_1,
    DurableRecordingStarlinkFullDwellQueryV0_1,
    DurableStarlinkFullDwellStoreV0_1,
    StarlinkFullDwellConflictError,
)
from leo_flow.analysis.recording.starlink_full_dwell_timeline import (
    DurableRecordingFullDwellTimelineQueryV0_1,
)
from leo_flow.contracts.core import RadioId, ReceiverChainId
from leo_flow.contracts.dashboard_full_dwell_timeline import (
    FullDwellTimelineQueryV0_1,
)
from leo_flow.contracts.starlink_full_dwell_response import StarlinkFullDwellQueryV0_1
from leo_flow.storage.filesystem import FileSystemBlobStore

from .test_starlink_full_dwell_response import full_dwell_result


class _Catalog:
    def __init__(self) -> None:
        self.item = None
        self.key = None

    def publish_starlink_full_dwell(
        self, projection, bundle_ref, recording_ref, *, idempotency_key, bundle
    ):
        del recording_ref, bundle
        candidate = CatalogedStarlinkFullDwellV0_1(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        elif self.item != candidate or self.key != idempotency_key:
            raise StarlinkFullDwellConflictError("conflicting replay")
        return self.item.ref

    def get_starlink_full_dwell(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_full_dwell(self, recording_id):
        return (
            self.item.ref
            if self.item is not None and self.item.ref.recording_id == recording_id
            else None
        )


def test_cas_first_exact_replay_conflict_and_bounded_independent_query(
    full_dwell_result, tmp_path
) -> None:
    _view, request, bundle = full_dwell_result
    catalog = _Catalog()
    store = DurableStarlinkFullDwellStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    first = store.publish(request, bundle, idempotency_key="fd:one")
    assert store.publish(request, bundle, idempotency_key="fd:one") == first
    with pytest.raises(StarlinkFullDwellConflictError):
        catalog.publish_starlink_full_dwell(
            replace(
                catalog.item.projection,
                point_count=catalog.item.projection.point_count + 1,
            ),
            first.bundle_ref,
            request.recording_object_ref,
            idempotency_key="fd:one",
            bundle=bundle,
        )
    target = bundle.streams[1]
    result = DurableRecordingStarlinkFullDwellQueryV0_1(
        store, catalog
    ).recording_starlink_full_dwell(
        StarlinkFullDwellQueryV0_1(
            bundle.recording_id,
            radio_ids=(RadioId(str(target.radio_id)),),
            receiver_chain_ids=(ReceiverChainId(str(target.receiver_chain_id)),),
            maximum_points=3,
        )
    )
    assert len(result.streams) == 1
    assert result.streams[0].radio_id == target.radio_id
    assert result.streams[0].receiver_chain_id == target.receiver_chain_id
    assert result.truncated and len(result.streams[0].points) == 3
    assert result.streams[0].prescreen_coverage_fraction == 1.0
    assert result.streams[0].exact_coverage_fraction < 1.0


def test_complete_timeline_exposes_every_persisted_prescreen_window(
    full_dwell_result, tmp_path
) -> None:
    _view, request, bundle = full_dwell_result
    catalog = _Catalog()
    store = DurableStarlinkFullDwellStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    store.publish(request, bundle, idempotency_key="fd:timeline")
    target = bundle.streams[0]
    result = DurableRecordingFullDwellTimelineQueryV0_1(
        store, catalog
    ).recording_full_dwell_timeline(
        FullDwellTimelineQueryV0_1(
            bundle.recording_id,
            radio_ids=(target.radio_id,),
            receiver_chain_ids=(target.receiver_chain_id,),
        )
    )
    assert len(result.streams) == 1
    stream = result.streams[0]
    assert stream.original_window_count == len(target.prescreen_windows)
    assert tuple(item.window_index for item in stream.windows) == tuple(
        item.window_index for item in target.prescreen_windows
    )
    assert tuple(item.mean_complex_power for item in stream.windows) == tuple(
        item.mean_complex_power for item in target.prescreen_windows
    )
    assert result.original_window_count == result.returned_window_count
    assert not result.truncated
    assert stream.prescreen_coverage_fraction == 1.0
    assert "power-prescreen-is-not-starlink-detection" in result.warnings
