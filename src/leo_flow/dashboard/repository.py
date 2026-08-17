"""Small read-model implementation of the frozen dashboard query port."""

from __future__ import annotations

import base64
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import CaptureBatchId, RadioId, RecordingId, UtcNs
from leo_flow.contracts.dashboard import (
    ActivityCount,
    ActivitySummary,
    FeatureView,
    ModelView,
    Page,
    RecordingDetail,
    RecordingSummary,
    StorageHealth,
    TimeRangeQuery,
    TrackView,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardView,
    CaptureBatchTimeRangeQuery,
)
from leo_flow.contracts.dashboard_recording import RecordingCaptureDetailViewV0_1
from leo_flow.contracts.dashboard_waterfall import RecordingWaterfallViewV0_1
from leo_flow.contracts.evaluation import DetectorEvaluationView

_CURSOR_VERSION: Final = 1
_UNAVAILABLE_STORAGE: Final = StorageHealth(False, None, None)


class DashboardNotFound(LookupError):
    pass


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class RecordingProjection:
    summary: RecordingSummary
    segment_count: int
    recording_object_available: bool
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.segment_count < 0 or self.projection_sequence < 0:
            raise ValueError("recording projection counts must be non-negative")


@dataclass(frozen=True)
class ActivityProjection:
    activity_id: str
    recording_id: RecordingId
    radio_id: RadioId
    kind: ActivityKind
    started_utc_ns: UtcNs
    projection_sequence: int

    def __post_init__(self) -> None:
        if not self.activity_id or self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class FeatureProjection:
    recording_id: RecordingId
    view: FeatureView
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class ModelProjection:
    view: ModelView
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class TrackProjection:
    radio_id: RadioId
    view: TrackView
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class CaptureBatchProjection:
    view: CaptureBatchDashboardView
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class RecordingCaptureDetailProjection:
    view: RecordingCaptureDetailViewV0_1
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


@dataclass(frozen=True)
class RecordingWaterfallProjection:
    view: RecordingWaterfallViewV0_1
    projection_sequence: int

    def __post_init__(self) -> None:
        if self.projection_sequence < 0:
            raise ValueError("projection sequence must be non-negative")


class InMemoryDashboardRepository:
    """DTO-only projection reader.

    The sequences are intentionally retained by reference so tests can model a
    projection receiving inserts between pages. The repository exposes no write
    method. Real adapters should issue equivalent keyset queries in read-only
    database transactions.
    """

    def __init__(
        self,
        *,
        recordings: Sequence[RecordingProjection] = (),
        activities: Sequence[ActivityProjection] = (),
        features: Sequence[FeatureProjection] = (),
        models: Sequence[ModelProjection] = (),
        evaluations: Sequence[DetectorEvaluationView] = (),
        tracks: Sequence[TrackProjection] = (),
        capture_batches: Sequence[CaptureBatchProjection] = (),
        recording_capture_details: Sequence[RecordingCaptureDetailProjection] = (),
        recording_waterfalls: Sequence[RecordingWaterfallProjection] = (),
        storage_health: StorageHealth = _UNAVAILABLE_STORAGE,
        page_size: int = 50,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._recordings = recordings
        self._activities = activities
        self._features = features
        self._models = models
        self._evaluations = evaluations
        self._tracks = tracks
        self._capture_batches = capture_batches
        self._recording_capture_details = recording_capture_details
        self._recording_waterfalls = recording_waterfalls
        self._storage_health = storage_health
        self._page_size = page_size

    def recent_recordings(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[RecordingSummary]:
        fingerprint = _time_query_fingerprint(query)
        state = _decode_cursor(cursor, "recordings", fingerprint)
        if state is not None and not _pair_cursor_key(state["after"]):
            raise InvalidCursor("cursor is invalid for this query")
        anchor = state["anchor"] if state else _max_sequence(self._recordings)
        after = tuple(state["after"]) if state else None
        snapshot = _latest_by(
            (row for row in self._recordings if row.projection_sequence <= anchor),
            lambda row: row.summary.recording_id,
        )
        eligible = [
            row
            for row in snapshot
            if _in_time_query(row.summary.started_utc_ns, row.summary.radio_id, query)
        ]
        eligible.sort(
            key=lambda row: (
                int(row.summary.started_utc_ns),
                str(row.summary.recording_id),
            ),
            reverse=True,
        )
        if after is not None:
            eligible = [row for row in eligible if _recording_key(row) < after]
        selected = eligible[: self._page_size]
        next_cursor = None
        if len(eligible) > len(selected):
            next_cursor = _encode_cursor(
                "recordings", fingerprint, anchor, list(_recording_key(selected[-1]))
            )
        return Page(tuple(row.summary for row in selected), next_cursor)

    def activity(self, query: TimeRangeQuery) -> ActivitySummary:
        activities = _latest_by(self._activities, lambda row: row.activity_id)
        counts = Counter(
            (row.radio_id, row.kind)
            for row in activities
            if _in_time_query(row.started_utc_ns, row.radio_id, query)
        )
        result = tuple(
            ActivityCount(radio_id, kind, count)
            for (radio_id, kind), count in sorted(
                counts.items(), key=lambda item: (str(item[0][0]), item[0][1].value)
            )
        )
        return ActivitySummary(query, result)

    def recording_detail(self, recording_id: RecordingId) -> RecordingDetail:
        matches = [
            row for row in self._recordings if row.summary.recording_id == recording_id
        ]
        if not matches:
            raise DashboardNotFound(f"recording {recording_id} was not found")
        row = max(matches, key=lambda item: item.projection_sequence)
        return RecordingDetail(
            row.summary, row.segment_count, row.recording_object_available
        )

    def recording_capture_detail(
        self, recording_id: RecordingId
    ) -> RecordingCaptureDetailViewV0_1:
        matches = [
            row
            for row in self._recording_capture_details
            if row.view.recording_id == recording_id
        ]
        if not matches:
            raise DashboardNotFound(
                f"capture detail for recording {recording_id} was not found"
            )
        return max(matches, key=lambda item: item.projection_sequence).view

    def recording_waterfall(
        self, recording_id: RecordingId
    ) -> RecordingWaterfallViewV0_1:
        matches = [
            row
            for row in self._recording_waterfalls
            if row.view.recording_id == recording_id
        ]
        if not matches:
            raise DashboardNotFound(
                f"waterfall for recording {recording_id} was not found"
            )
        return max(matches, key=lambda item: item.projection_sequence).view

    def recording_features(
        self, recording_id: RecordingId, selector: str, cursor: str | None = None
    ) -> Page[FeatureView]:
        if not selector:
            raise ValueError("feature selector cannot be empty")
        self.recording_detail(recording_id)
        fingerprint = f"{recording_id}:{selector}"
        state = _decode_cursor(cursor, "features", fingerprint)
        if state is not None and not isinstance(state["after"], str):
            raise InvalidCursor("cursor is invalid for this query")
        candidates = [row for row in self._features if row.recording_id == recording_id]
        anchor = state["anchor"] if state else _max_sequence(candidates)
        after = state["after"] if state else None
        snapshot = _latest_by(
            (row for row in candidates if row.projection_sequence <= anchor),
            lambda row: row.view.feature_id,
        )
        eligible = [
            row
            for row in snapshot
            if (selector == "*" or row.view.method_id == selector)
            and (after is None or row.view.feature_id > after)
        ]
        eligible.sort(key=lambda row: row.view.feature_id)
        selected = eligible[: self._page_size]
        next_cursor = None
        if len(eligible) > len(selected):
            next_cursor = _encode_cursor(
                "features", fingerprint, anchor, selected[-1].view.feature_id
            )
        return Page(tuple(row.view for row in selected), next_cursor)

    def model_snapshot(self, model_id_or_release_alias: str) -> ModelView:
        if not model_id_or_release_alias:
            raise ValueError("model ID or release alias cannot be empty")
        latest_models = _latest_by(self._models, lambda row: row.view.model_snapshot_id)
        matches = [
            row
            for row in latest_models
            if str(row.view.model_snapshot_id) == model_id_or_release_alias
            or row.view.release_alias == model_id_or_release_alias
        ]
        if not matches:
            raise DashboardNotFound(
                f"model or release {model_id_or_release_alias} was not found"
            )
        identities = {row.view.model_snapshot_id for row in matches}
        if len(identities) != 1:
            raise RuntimeError("model release alias is ambiguous")
        return cast(
            ModelView, max(matches, key=lambda row: row.projection_sequence).view
        )

    def detector_evaluation(
        self, evaluation_id_or_run_id: str
    ) -> DetectorEvaluationView:
        if not evaluation_id_or_run_id:
            raise ValueError("evaluation ID or run ID cannot be empty")
        matches = [
            view
            for view in self._evaluations
            if str(view.ref.evaluation_id) == evaluation_id_or_run_id
            or str(view.ref.run_id) == evaluation_id_or_run_id
        ]
        if not matches:
            raise DashboardNotFound(
                f"detector evaluation {evaluation_id_or_run_id} was not found"
            )
        if len(matches) != 1:
            raise RuntimeError("detector evaluation identity is ambiguous")
        return matches[0]

    def tracks(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[TrackView]:
        fingerprint = _time_query_fingerprint(query)
        state = _decode_cursor(cursor, "tracks", fingerprint)
        if state is not None and not _pair_cursor_key(state["after"]):
            raise InvalidCursor("cursor is invalid for this query")
        anchor = state["anchor"] if state else _max_sequence(self._tracks)
        after = tuple(state["after"]) if state else None
        snapshot = _latest_by(
            (row for row in self._tracks if row.projection_sequence <= anchor),
            lambda row: row.view.track_id,
        )
        eligible = [
            row
            for row in snapshot
            if _in_time_query(row.view.started_utc_ns, row.radio_id, query)
        ]
        eligible.sort(
            key=lambda row: (int(row.view.started_utc_ns), row.view.track_id),
            reverse=True,
        )
        if after is not None:
            eligible = [row for row in eligible if _track_key(row) < after]
        selected = eligible[: self._page_size]
        next_cursor = None
        if len(eligible) > len(selected):
            next_cursor = _encode_cursor(
                "tracks", fingerprint, anchor, list(_track_key(selected[-1]))
            )
        return Page(tuple(row.view for row in selected), next_cursor)

    def storage_health(self) -> StorageHealth:
        return self._storage_health

    def recent_capture_batches(
        self, query: CaptureBatchTimeRangeQuery, cursor: str | None = None
    ) -> Page[CaptureBatchDashboardView]:
        fingerprint = f"{int(query.start_utc_ns)}:{int(query.stop_utc_ns)}"
        state = _decode_cursor(cursor, "capture_batches", fingerprint)
        if state is not None and not _pair_cursor_key(state["after"]):
            raise InvalidCursor("cursor is invalid for this query")
        anchor = state["anchor"] if state else _max_sequence(self._capture_batches)
        after = tuple(state["after"]) if state else None
        snapshot = _latest_by(
            (row for row in self._capture_batches if row.projection_sequence <= anchor),
            lambda row: row.view.batch_id,
        )
        eligible = [
            row
            for row in snapshot
            if query.start_utc_ns <= row.view.requested_start_utc_ns < query.stop_utc_ns
        ]
        eligible.sort(key=_capture_batch_key, reverse=True)
        if after is not None:
            eligible = [row for row in eligible if _capture_batch_key(row) < after]
        selected = eligible[: self._page_size]
        next_cursor = None
        if len(eligible) > len(selected):
            next_cursor = _encode_cursor(
                "capture_batches",
                fingerprint,
                anchor,
                list(_capture_batch_key(selected[-1])),
            )
        return Page(tuple(row.view for row in selected), next_cursor)

    def capture_batch(self, batch_id: CaptureBatchId) -> CaptureBatchDashboardView:
        matches = [
            row for row in self._capture_batches if row.view.batch_id == batch_id
        ]
        if not matches:
            raise DashboardNotFound(f"capture batch {batch_id} was not found")
        latest = max(matches, key=lambda row: row.projection_sequence)
        if any(
            row != latest and row.projection_sequence == latest.projection_sequence
            for row in matches
        ):
            raise RuntimeError("projection sequence collision")
        return latest.view


def _in_time_query(started: UtcNs, radio_id: RadioId, query: TimeRangeQuery) -> bool:
    return query.start_utc_ns <= started < query.stop_utc_ns and (
        not query.radio_ids or radio_id in query.radio_ids
    )


def _recording_key(row: RecordingProjection) -> tuple[int, str]:
    return int(row.summary.started_utc_ns), str(row.summary.recording_id)


def _track_key(row: TrackProjection) -> tuple[int, str]:
    return int(row.view.started_utc_ns), row.view.track_id


def _capture_batch_key(row: CaptureBatchProjection) -> tuple[int, str]:
    return int(row.view.requested_start_utc_ns), str(row.view.batch_id)


def _max_sequence(rows: Sequence[Any]) -> int:
    return max((row.projection_sequence for row in rows), default=-1)


def _latest_by(rows: Any, key: Any) -> list[Any]:
    latest: dict[Any, Any] = {}
    for row in rows:
        identity = key(row)
        current = latest.get(identity)
        if current is None or row.projection_sequence > current.projection_sequence:
            latest[identity] = row
        elif row.projection_sequence == current.projection_sequence and row != current:
            raise RuntimeError("projection sequence collision")
    return list(latest.values())


def _time_query_fingerprint(query: TimeRangeQuery) -> str:
    radios = ",".join(str(radio_id) for radio_id in query.radio_ids)
    return f"{int(query.start_utc_ns)}:{int(query.stop_utc_ns)}:{radios}"


def _pair_cursor_key(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and isinstance(value[1], str)
    )


def _encode_cursor(kind: str, fingerprint: str, anchor: int, after: Any) -> str:
    encoded = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": kind,
            "query": fingerprint,
            "anchor": anchor,
            "after": after,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(
    cursor: str | None, expected_kind: str, expected_fingerprint: str
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        )
        if (
            set(value) != {"v", "kind", "query", "anchor", "after"}
            or value["v"] != _CURSOR_VERSION
            or value["kind"] != expected_kind
            or value["query"] != expected_fingerprint
            or isinstance(value["anchor"], bool)
            or not isinstance(value["anchor"], int)
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCursor("cursor is invalid for this query") from error
    return cast(dict[str, Any], value)
