"""Read-only PostgreSQL adapter for the frozen dashboard query port."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final, cast

import psycopg

from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import ModelSnapshotId, RadioId, RecordingId, UtcNs
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
from leo_flow.contracts.evaluation import DetectorEvaluationView
from leo_flow.dashboard import DashboardNotFound, InvalidCursor

from . import dashboard_postgres_sql as sql
from .evaluation_dashboard_postgres import PostgresEvaluationDashboard

_CURSOR_VERSION: Final = 1
_MAX_PAGE_SIZE: Final = 200
ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresDashboardRepository:
    """Query normalized projections without gaining any write capability."""

    def __init__(self, connect: ConnectionFactory, *, page_size: int = 50) -> None:
        if not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        self._connect = connect
        self._page_size = page_size
        self._evaluations = PostgresEvaluationDashboard(connect)

    def recent_recordings(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[RecordingSummary]:
        fingerprint = _time_fingerprint(query)
        state = _decode_cursor(cursor, "recordings", fingerprint, pair=True)
        with self._reader() as connection:
            anchor = _anchor(connection) if state is None else state["anchor"]
            after = None if state is None else cast(list[object], state["after"])
            rows = connection.execute(
                sql.RECENT_RECORDINGS_SQL,
                {
                    **_time_parameters(query),
                    "anchor": anchor,
                    "after_started": None if after is None else after[0],
                    "after_id": None if after is None else after[1],
                    "limit": self._page_size + 1,
                },
            ).fetchall()
        selected = rows[: self._page_size]
        next_cursor = None
        if len(rows) > self._page_size:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "recordings",
                fingerprint,
                anchor,
                [last["started_utc_ns"], last["recording_id"]],
            )
        return Page(tuple(_summary(row) for row in selected), next_cursor)

    def activity(self, query: TimeRangeQuery) -> ActivitySummary:
        with self._reader() as connection:
            rows = connection.execute(
                sql.ACTIVITY_SQL, _time_parameters(query)
            ).fetchall()
        return ActivitySummary(
            query,
            tuple(
                ActivityCount(
                    RadioId(str(row["radio_id"])),
                    ActivityKind(str(row["kind"])),
                    _int(row["activity_count"], "activity_count"),
                )
                for row in rows
            ),
        )

    def recording_detail(self, recording_id: RecordingId) -> RecordingDetail:
        with self._reader() as connection:
            row = connection.execute(
                sql.RECORDING_DETAIL_SQL, {"recording_id": str(recording_id)}
            ).fetchone()
        if row is None:
            raise DashboardNotFound(f"recording {recording_id} was not found")
        return RecordingDetail(
            _summary(row),
            _int(row["segment_count"], "segment_count"),
            _bool(row["recording_object_available"], "recording_object_available"),
        )

    def recording_features(
        self, recording_id: RecordingId, selector: str, cursor: str | None = None
    ) -> Page[FeatureView]:
        if not selector:
            raise ValueError("feature selector cannot be empty")
        fingerprint = f"{recording_id}:{selector}"
        state = _decode_cursor(cursor, "features", fingerprint, pair=False)
        with self._reader() as connection:
            exists = connection.execute(
                sql.RECORDING_DETAIL_SQL, {"recording_id": str(recording_id)}
            ).fetchone()
            if exists is None:
                raise DashboardNotFound(f"recording {recording_id} was not found")
            anchor = _anchor(connection) if state is None else state["anchor"]
            after = None if state is None else state["after"]
            rows = connection.execute(
                sql.FEATURES_SQL,
                {
                    "recording_id": str(recording_id),
                    "selector": selector,
                    "anchor": anchor,
                    "after_id": after,
                    "limit": self._page_size + 1,
                },
            ).fetchall()
        selected = rows[: self._page_size]
        next_cursor = None
        if len(rows) > self._page_size:
            next_cursor = _encode_cursor(
                "features", fingerprint, anchor, selected[-1]["feature_id"]
            )
        return Page(tuple(_feature(row) for row in selected), next_cursor)

    def model_snapshot(self, model_id_or_release_alias: str) -> ModelView:
        if not model_id_or_release_alias:
            raise ValueError("model ID or release alias cannot be empty")
        with self._reader() as connection:
            rows = connection.execute(
                sql.MODEL_SQL, {"identity": model_id_or_release_alias}
            ).fetchall()
        if not rows:
            raise DashboardNotFound(
                f"model or release {model_id_or_release_alias} was not found"
            )
        identities = {str(row["model_snapshot_id"]) for row in rows}
        if len(identities) != 1:
            raise RuntimeError("model release alias is ambiguous")
        row = rows[0]
        warnings = row["warnings"]
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise TypeError("database model warnings are invalid")
        return ModelView(
            ModelSnapshotId(str(row["model_snapshot_id"])),
            None if row["release_alias"] is None else str(row["release_alias"]),
            _int(row["parameter_count"], "parameter_count"),
            tuple(warnings),
        )

    def detector_evaluation(
        self, evaluation_id_or_run_id: str
    ) -> DetectorEvaluationView:
        return self._evaluations.detector_evaluation(evaluation_id_or_run_id)

    def tracks(
        self, query: TimeRangeQuery, cursor: str | None = None
    ) -> Page[TrackView]:
        fingerprint = _time_fingerprint(query)
        state = _decode_cursor(cursor, "tracks", fingerprint, pair=True)
        with self._reader() as connection:
            anchor = _anchor(connection) if state is None else state["anchor"]
            after = None if state is None else cast(list[object], state["after"])
            rows = connection.execute(
                sql.TRACKS_SQL,
                {
                    **_time_parameters(query),
                    "anchor": anchor,
                    "after_started": None if after is None else after[0],
                    "after_id": None if after is None else after[1],
                    "limit": self._page_size + 1,
                },
            ).fetchall()
        selected = rows[: self._page_size]
        next_cursor = None
        if len(rows) > self._page_size:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "tracks",
                fingerprint,
                anchor,
                [last["started_utc_ns"], last["track_id"]],
            )
        return Page(tuple(_track(row) for row in selected), next_cursor)

    def storage_health(self) -> StorageHealth:
        with self._reader() as connection:
            row = connection.execute(sql.STORAGE_HEALTH_SQL).fetchone()
        if row is None:
            return StorageHealth(False, None, None)
        return StorageHealth(
            _bool(row["available"], "available"),
            _optional_int(row["total_bytes"], "total_bytes"),
            _optional_int(row["free_bytes"], "free_bytes"),
        )

    @contextmanager
    def _reader(self) -> Iterator[psycopg.Connection[dict[str, object]]]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            state = connection.execute("SHOW transaction_read_only").fetchone()
            if state is None or state["transaction_read_only"] != "on":
                raise RuntimeError("dashboard transaction is not read-only")
            yield connection


def _summary(row: dict[str, object]) -> RecordingSummary:
    kinds = row["activity_kinds"]
    if not isinstance(kinds, list):
        raise TypeError("database activity kinds are invalid")
    return RecordingSummary(
        RecordingId(str(row["recording_id"])),
        RadioId(str(row["radio_id"])),
        UtcNs(_int(row["started_utc_ns"], "started_utc_ns")),
        UtcNs(_int(row["finished_utc_ns"], "finished_utc_ns")),
        tuple(ActivityKind(str(kind)) for kind in kinds),
        str(row["analysis_state"]),
    )


def _feature(row: dict[str, object]) -> FeatureView:
    score = row["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise TypeError("database score is invalid")
    return FeatureView(
        str(row["feature_id"]),
        str(row["method_id"]),
        float(score),
        str(row["score_semantics"]),
    )


def _track(row: dict[str, object]) -> TrackView:
    return TrackView(
        str(row["track_id"]),
        ModelSnapshotId(str(row["model_snapshot_id"])),
        UtcNs(_int(row["started_utc_ns"], "started_utc_ns")),
        UtcNs(_int(row["finished_utc_ns"], "finished_utc_ns")),
    )


def _anchor(connection: psycopg.Connection[dict[str, object]]) -> int:
    row = connection.execute(sql.PROJECTION_ANCHOR_SQL).fetchone()
    return -1 if row is None else _int(row["last_value"], "projection anchor")


def _time_parameters(query: TimeRangeQuery) -> dict[str, object]:
    return {
        "start_utc_ns": int(query.start_utc_ns),
        "stop_utc_ns": int(query.stop_utc_ns),
        "radio_ids": [str(radio_id) for radio_id in query.radio_ids],
    }


def _time_fingerprint(query: TimeRangeQuery) -> str:
    radios = ",".join(str(radio_id) for radio_id in query.radio_ids)
    return f"{int(query.start_utc_ns)}:{int(query.stop_utc_ns)}:{radios}"


def _encode_cursor(kind: str, query: str, anchor: int, after: object) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": kind,
            "query": query,
            "anchor": anchor,
            "after": after,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    cursor: str | None, kind: str, query: str, *, pair: bool
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        state = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        )
        after = state["after"]
        valid_after = (
            isinstance(after, list)
            and len(after) == 2
            and isinstance(after[0], int)
            and not isinstance(after[0], bool)
            and isinstance(after[1], str)
            if pair
            else isinstance(after, str)
        )
        if (
            set(state) != {"v", "kind", "query", "anchor", "after"}
            or state["v"] != _CURSOR_VERSION
            or state["kind"] != kind
            or state["query"] != query
            or isinstance(state["anchor"], bool)
            or not isinstance(state["anchor"], int)
            or not valid_after
        ):
            raise ValueError
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise InvalidCursor("cursor is invalid for this query") from error
    return cast(dict[str, Any], state)


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {field} is not an integer")
    return value


def _optional_int(value: object, field: str) -> int | None:
    return None if value is None else _int(value, field)


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"database {field} is not a boolean")
    return value
