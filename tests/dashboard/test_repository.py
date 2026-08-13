from __future__ import annotations

import pytest

from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import RadioId, RecordingId, UtcNs
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.dashboard.repository import (
    DashboardNotFound,
    InMemoryDashboardRepository,
)

from ._fixtures import RADIO_A, RADIO_B, recording, repository


def query(start: int = 100, stop: int = 140, *radios: RadioId) -> TimeRangeQuery:
    return TimeRangeQuery(UtcNs(start), UtcNs(stop), radios)


def test_recent_recordings_filter_and_half_open_start_boundaries() -> None:
    repo = repository(page_size=10)
    page = repo.recent_recordings(query(100, 130, RADIO_A))
    assert [str(item.recording_id) for item in page.items] == ["rec_2", "rec_1"]
    assert all(item.radio_id == RADIO_A for item in page.items)
    assert "rec_4" not in {str(item.recording_id) for item in page.items}


def test_recording_pagination_is_snapshot_stable_under_inserts() -> None:
    rows = [
        recording("rec_1", RADIO_A, 100, "complete", 1),
        recording("rec_2", RADIO_A, 110, "complete", 2),
        recording("rec_3", RADIO_A, 120, "complete", 3),
    ]
    repo = InMemoryDashboardRepository(recordings=rows, page_size=2)
    first = repo.recent_recordings(query())
    assert [str(item.recording_id) for item in first.items] == ["rec_3", "rec_2"]
    rows.extend(
        [
            recording("rec_newer", RADIO_A, 130, "complete", 4),
            recording("rec_between", RADIO_A, 105, "complete", 5),
        ]
    )
    second = repo.recent_recordings(query(), first.next_cursor)
    assert [str(item.recording_id) for item in second.items] == ["rec_1"]


def test_cursor_cannot_be_reused_for_another_filter_or_endpoint() -> None:
    repo = repository()
    cursor = repo.recent_recordings(query()).next_cursor
    with pytest.raises(ValueError, match="invalid"):
        repo.recent_recordings(query(100, 130), cursor)
    with pytest.raises(ValueError, match="invalid"):
        repo.tracks(query(), cursor)


def test_activity_counts_each_typed_activity_at_half_open_start() -> None:
    summary = repository().activity(query(110, 131))
    observed = {(item.radio_id, item.kind): item.count for item in summary.counts}
    assert observed == {
        (RADIO_A, ActivityKind.SCAN): 2,
        (RADIO_A, ActivityKind.DWELL): 1,
        (RADIO_B, ActivityKind.DWELL): 1,
    }
    at_stop = repository().activity(query(100, 130))
    assert sum(item.count for item in at_stop.counts) == 3


def test_details_expose_states_and_blob_availability_without_interpretation() -> None:
    repo = repository()
    states = {
        identity: repo.recording_detail(RecordingId(identity)).summary.analysis_state
        for identity in ("rec_1", "rec_2", "rec_3", "rec_4")
    }
    assert states == {
        "rec_1": "complete",
        "rec_2": "partial",
        "rec_3": "failed",
        "rec_4": "superseded",
    }
    assert not repo.recording_detail(RecordingId("rec_4")).recording_object_available
    with pytest.raises(DashboardNotFound):
        repo.recording_detail(RecordingId("rec_absent"))


def test_features_use_explicit_method_selector_and_pagination() -> None:
    repo = repository(page_size=1)
    first = repo.recording_features(RecordingId("rec_1"), "glrt32")
    second = repo.recording_features(RecordingId("rec_1"), "glrt32", first.next_cursor)
    assert [item.feature_id for item in (*first.items, *second.items)] == [
        "feature_a",
        "feature_c",
    ]
    assert repo.recording_features(RecordingId("rec_1"), "missing").items == ()
    with pytest.raises(DashboardNotFound):
        repo.recording_features(RecordingId("rec_absent"), "*")


def test_models_require_explicit_id_or_release_alias_not_latest() -> None:
    repo = repository()
    assert (
        repo.model_snapshot("production").model_snapshot_id
        == repo.model_snapshot("model_a").model_snapshot_id
    )
    assert repo.model_snapshot("model_b").release_alias is None
    with pytest.raises(DashboardNotFound):
        repo.model_snapshot("latest")


def test_tracks_apply_time_radio_filter_and_keyset_pagination() -> None:
    repo = repository(page_size=1)
    first = repo.tracks(query(100, 140, RADIO_A))
    second = repo.tracks(query(100, 140, RADIO_A), first.next_cursor)
    assert [item.track_id for item in (*first.items, *second.items)] == [
        "track_3",
        "track_1",
    ]
    assert all(item.track_id != "track_2" for item in (*first.items, *second.items))


def test_empty_repository_and_unavailable_storage_are_explicit() -> None:
    repo = InMemoryDashboardRepository()
    assert repo.recent_recordings(query()).items == ()
    assert repo.activity(query()).counts == ()
    assert repo.tracks(query()).items == ()
    assert not repo.storage_health().available
    assert repo.storage_health().total_bytes is None
    assert repo.storage_health().free_bytes is None


def test_latest_projection_version_is_selected_before_filtering() -> None:
    rows = [
        recording("rec_1", RADIO_A, 110, "partial", 1),
        recording("rec_1", RADIO_B, 200, "complete", 2),
    ]
    repo = InMemoryDashboardRepository(recordings=rows)
    assert repo.recent_recordings(query(100, 120, RADIO_A)).items == ()
    detail = repo.recording_detail(RecordingId("rec_1"))
    assert detail.summary.radio_id == RADIO_B
    assert detail.summary.analysis_state == "complete"


def test_projection_sequence_collision_fails_instead_of_choosing_by_input_order() -> (
    None
):
    rows = [
        recording("rec_1", RADIO_A, 110, "partial", 1),
        recording("rec_1", RADIO_B, 110, "complete", 1),
    ]
    repo = InMemoryDashboardRepository(recordings=rows)
    with pytest.raises(RuntimeError, match="collision"):
        repo.recent_recordings(query())
