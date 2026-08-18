from __future__ import annotations

from leo_flow.analysis.recording.starlink_full_dwell_timeline import (
    DurableRecordingPromptFullDwellTimelineQueryV0_1,
    PreferPromptFullDwellTimelineQueryV0_1,
)
from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    DurableFullDwellTimelineStoreV0_1,
)
from leo_flow.contracts.dashboard_full_dwell_timeline import FullDwellTimelineQueryV0_1
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.recording_analysis.test_starlink_full_dwell_timeline_persistence import (
    _Catalog,
)
from tests.recording_analysis.test_starlink_full_dwell_timeline_product import _case


class _LatestCatalog(_Catalog):
    def latest_full_dwell_timeline(self, recording_id):
        if self.item is None or self.item.projection.recording_id != recording_id:
            return None
        return self.item.ref


class _Fallback:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls = 0

    def recording_full_dwell_timeline(self, query):
        self.calls += 1
        return self.result


def test_v20_prefers_prompt_complete_product_and_preserves_sparse_markers(
    tmp_path,
) -> None:
    _view, request, bundle = _case(19)
    catalog = _LatestCatalog()
    store = DurableFullDwellTimelineStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    store.publish(request, bundle, idempotency_key="prompt-projection")
    prompt = DurableRecordingPromptFullDwellTimelineQueryV0_1(store, catalog)
    legacy = _Fallback()
    view = PreferPromptFullDwellTimelineQueryV0_1(
        prompt, legacy
    ).recording_full_dwell_timeline(FullDwellTimelineQueryV0_1(request.recording_id))
    assert legacy.calls == 0
    assert view.original_window_count == 6
    assert view.returned_window_count == 6
    assert all(stream.prescreen_coverage_fraction == 1.0 for stream in view.streams)
    assert all(
        any(window.selected_for_exact_refinement for window in stream.windows)
        for stream in view.streams
    )
    assert "prompt-base-product-independent-of-exact-overlay" in view.warnings


def test_v20_falls_back_to_published_v15_when_prompt_product_is_pending() -> None:
    expected = object()
    prompt = _Fallback()
    legacy = _Fallback(expected)

    def missing(_query):
        from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
            StarlinkFullDwellNotFoundError,
        )

        raise StarlinkFullDwellNotFoundError("pending")

    prompt.recording_full_dwell_timeline = missing
    query = FullDwellTimelineQueryV0_1(_case(19)[1].recording_id)
    assert (
        PreferPromptFullDwellTimelineQueryV0_1(
            prompt, legacy
        ).recording_full_dwell_timeline(query)
        is expected
    )
    assert legacy.calls == 1
