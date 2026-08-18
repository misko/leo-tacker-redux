from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_symbolwise_replay_product_codec import (
    MalformedStarlinkSymbolwiseReplayError,
    decode_starlink_symbolwise_recording_bundle,
    decode_starlink_symbolwise_replay_request,
    encode_starlink_symbolwise_recording_bundle,
    encode_starlink_symbolwise_replay_request,
)
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
    CatalogedStarlinkSymbolwiseReplayV0_1,
    DurableRecordingStarlinkSymbolwiseReplayQueryV0_1,
    DurableStarlinkSymbolwiseReplayStoreV0_1,
    StarlinkSymbolwiseReplayConflictError,
)
from leo_flow.contracts.core import ReceiverChainId
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    StarlinkSymbolwiseReplayPublicationFenceV0_1,
    StarlinkSymbolwiseReplayQueryV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .symbolwise_product_fixtures import frequency_center, recording_bundle, request


class _Catalog:
    def __init__(self) -> None:
        self.item = None
        self.key = None
        self.fence = None

    def publish_starlink_symbolwise_replay(
        self,
        projection,
        bundle_ref,
        recording_ref,
        *,
        lease_fence,
        idempotency_key,
    ):
        del recording_ref
        candidate = CatalogedStarlinkSymbolwiseReplayV0_1(projection, bundle_ref)
        if self.item is None:
            self.item = candidate
            self.key = idempotency_key
            self.fence = lease_fence
        elif (
            self.item != candidate
            or self.key != idempotency_key
            or self.fence != lease_fence
        ):
            raise StarlinkSymbolwiseReplayConflictError("conflicting replay")
        return self.item.ref

    def get_starlink_symbolwise_replay(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_symbolwise_replay(self, recording_id):
        return (
            self.item.ref
            if self.item is not None and self.item.ref.recording_id == recording_id
            else None
        )


def _fence() -> StarlinkSymbolwiseReplayPublicationFenceV0_1:
    return StarlinkSymbolwiseReplayPublicationFenceV0_1(
        "slsymwork_" + "a" * 32, "worker:lease_1", 1
    )


def test_plan_is_fixed_explicit_and_request_codec_is_canonical() -> None:
    replay_request = request()
    payload = encode_starlink_symbolwise_replay_request(replay_request)

    assert decode_starlink_symbolwise_replay_request(payload) == replay_request
    assert replay_request.plan.admission_mode == "explicit-on-demand-or-backfill"
    assert replay_request.plan.maximum_windows == 600
    assert replay_request.plan.surrogate_count == 4
    with pytest.raises(ValueError, match="cannot be default capture work"):
        replace(replay_request.plan, admission_mode="every-capture")
    with pytest.raises(MalformedStarlinkSymbolwiseReplayError):
        decode_starlink_symbolwise_replay_request(payload + b"\n")


def test_recording_bundle_codec_preserves_every_candidate_trace() -> None:
    replay_request = request()
    bundle = recording_bundle(replay_request)
    payload = encode_starlink_symbolwise_recording_bundle(bundle)

    decoded = decode_starlink_symbolwise_recording_bundle(payload)
    assert decoded == bundle
    assert decoded.total_window_count == 600
    assert decoded.total_pattern_evidence_count == 3_000
    assert decoded.streams[0].analyzed_union_sample_count == 15_000_000
    assert decoded.streams[0].coverage_fraction == 0.1
    assert all(len(window.patterns) == 5 for window in decoded.streams[0].windows)
    assert decoded.candidates_only


def test_cas_first_exact_replay_and_bounded_query_preserve_endpoints(
    tmp_path,
) -> None:
    replay_request = request()
    bundle = recording_bundle(replay_request)
    catalog = _Catalog()
    store = DurableStarlinkSymbolwiseReplayStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    result_ref = store.publish(
        replay_request,
        bundle,
        lease_fence=_fence(),
        idempotency_key="symbolwise:test:one",
    )
    assert (
        store.publish(
            replay_request,
            bundle,
            lease_fence=_fence(),
            idempotency_key="symbolwise:test:one",
        )
        == result_ref
    )

    result = DurableRecordingStarlinkSymbolwiseReplayQueryV0_1(
        store, catalog
    ).recording_starlink_symbolwise_replay(
        StarlinkSymbolwiseReplayQueryV0_1(
            bundle.recording_id,
            receiver_chain_ids=(ReceiverChainId("rx_lnb_c"),),
            maximum_windows=7,
        )
    )
    assert len(result.streams) == 1
    assert result.original_window_count == 600
    assert result.shown_window_count == 7
    assert result.truncated
    assert result.selection_rule == "even-index-preserving"
    assert tuple(window.window_index for window in result.streams[0].windows)[::6] == (
        0,
        599,
    )
    assert all(len(window.patterns) == 5 for window in result.streams[0].windows)
    assert result.candidates_only


def test_publication_fence_is_positive_and_portable() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        replace(_fence(), lease_generation=0)
    with pytest.raises(ValueError, match="portable token"):
        replace(_fence(), lease_token="lease with spaces")


def test_query_window_budget_is_global_across_multiple_receivers(tmp_path) -> None:
    first_request = request()
    first = first_request.stream_selections[0]
    second = replace(
        first,
        receiver_chain_id=ReceiverChainId("rx_lnb_d"),
        frequency_center=frequency_center(598_100.0, path=b"physical-path-d"),
    )
    replay_request = replace(first_request, stream_selections=(first, second))
    bundle = recording_bundle(replay_request)
    catalog = _Catalog()
    store = DurableStarlinkSymbolwiseReplayStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    store.publish(
        replay_request,
        bundle,
        lease_fence=_fence(),
        idempotency_key="symbolwise:test:two-stream",
    )

    result = DurableRecordingStarlinkSymbolwiseReplayQueryV0_1(
        store, catalog
    ).recording_starlink_symbolwise_replay(
        StarlinkSymbolwiseReplayQueryV0_1(
            bundle.recording_id,
            maximum_windows=1,
        )
    )

    assert len(result.streams) == 2
    assert result.original_window_count == 1_200
    assert result.shown_window_count == 1
    assert tuple(len(stream.windows) for stream in result.streams) == (1, 0)
