"""PostgreSQL publication and V0.1 reads for dashboard capture batches."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Final, Protocol, cast

import psycopg
from psycopg.types.json import Jsonb

from leo_flow.application.projection_writers import (
    AnalysisProjectionWriter,
    FeatureProjectionCommand,
    ModelProjectionCommand,
    ModelReleaseProjectionCommand,
    ProjectionReceipt,
    TrackProjectionCommand,
)
from leo_flow.contracts.capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard import Page, StorageHealth
from leo_flow.contracts.dashboard_batch import (
    CaptureAttemptDashboardView,
    CaptureBatchDashboardView,
    CaptureBatchTimeRangeQuery,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.dashboard import DashboardNotFound, InvalidCursor

from . import dashboard_batch_postgres_sql as sql

_CURSOR_VERSION: Final = 1
_MAX_PAGE_SIZE: Final = 200
ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCaptureBatchProjectionWriter:
    """Publish a validated public DTO through the sole database write routine."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(self, view: CaptureBatchDashboardView) -> int:
        payload = json.loads(canonical_json_bytes(view))
        with self._connect() as connection:
            row = connection.execute(
                sql.PUBLISH_BATCH_SQL, {"view": Jsonb(payload)}
            ).fetchone()
        if row is None:
            raise RuntimeError("capture batch projection returned no receipt")
        return _int(row["projection_sequence"], "projection_sequence")


class CaptureBatchProjectionPublisher(Protocol):
    def publish(self, view: CaptureBatchDashboardView) -> int: ...


class PostgresBatchAwareAnalysisProjectionWriter:
    """Delegate analysis projection, then converge every matching batch view."""

    def __init__(
        self,
        connect: ConnectionFactory,
        delegate: AnalysisProjectionWriter,
        *,
        batch_publisher: CaptureBatchProjectionPublisher | None = None,
    ) -> None:
        self._connect = connect
        self._delegate = delegate
        self._batch_publisher = batch_publisher or PostgresCaptureBatchProjectionWriter(
            connect
        )

    def project_features(self, command: FeatureProjectionCommand) -> ProjectionReceipt:
        receipt = self._delegate.project_features(command)
        views = self._resolve_recording_batches(command.bundle.recording_id)
        for view in views:
            updated_attempts = tuple(
                replace(
                    attempt,
                    analysis_state=DashboardAnalysisState.COMPLETE,
                    analysis_result_available=True,
                )
                if attempt.recording_id == command.bundle.recording_id
                else attempt
                for attempt in view.attempts
            )
            if updated_attempts != view.attempts:
                self._batch_publisher.publish(
                    replace(
                        view,
                        attempts=cast(
                            tuple[
                                CaptureAttemptDashboardView,
                                CaptureAttemptDashboardView,
                            ],
                            updated_attempts,
                        ),
                    )
                )
        return receipt

    def project_model(self, command: ModelProjectionCommand) -> ProjectionReceipt:
        return self._delegate.project_model(command)

    def project_model_release(
        self, command: ModelReleaseProjectionCommand
    ) -> ProjectionReceipt:
        return self._delegate.project_model_release(command)

    def project_track(self, command: TrackProjectionCommand) -> ProjectionReceipt:
        return self._delegate.project_track(command)

    def project_storage_health(self, health: StorageHealth) -> ProjectionReceipt:
        return self._delegate.project_storage_health(health)

    def _resolve_recording_batches(
        self, recording_id: RecordingId
    ) -> tuple[CaptureBatchDashboardView, ...]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                sql.RESOLVE_BATCHES_FOR_RECORDING_SQL,
                {"recording_id": str(recording_id)},
            ).fetchall()
        result = tuple(_view_from_document(row["semantic_view"]) for row in rows)
        if any(
            str(view.batch_id) != str(row["batch_id"])
            for view, row in zip(result, rows)
        ):
            raise RuntimeError("resolved capture batch projection identity is invalid")
        return result


class PostgresCaptureBatchDashboardRepository:
    """Read only the two batch projection tables through the public V0.1 port."""

    def __init__(self, connect: ConnectionFactory, *, page_size: int = 50) -> None:
        if not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        self._connect = connect
        self._page_size = page_size

    def recent_capture_batches(
        self, query: CaptureBatchTimeRangeQuery, cursor: str | None = None
    ) -> Page[CaptureBatchDashboardView]:
        fingerprint = f"{int(query.start_utc_ns)}:{int(query.stop_utc_ns)}"
        state = _decode_cursor(cursor, fingerprint)
        with self._reader() as connection:
            anchor = _anchor(connection) if state is None else state["anchor"]
            after = None if state is None else cast(list[object], state["after"])
            rows = connection.execute(
                sql.RECENT_BATCHES_SQL,
                {
                    "anchor": anchor,
                    "start_utc_ns": int(query.start_utc_ns),
                    "stop_utc_ns": int(query.stop_utc_ns),
                    "after_started": None if after is None else after[0],
                    "after_id": None if after is None else after[1],
                    "limit": self._page_size + 1,
                },
            ).fetchall()
            selected = rows[: self._page_size]
            attempts = _attempts_by_sequence(connection, selected)
        views = tuple(_batch(row, attempts) for row in selected)
        next_cursor = None
        if len(rows) > self._page_size:
            last = selected[-1]
            next_cursor = _encode_cursor(
                fingerprint,
                anchor,
                [last["requested_start_utc_ns"], last["batch_id"]],
            )
        return Page(views, next_cursor)

    def capture_batch(self, batch_id: CaptureBatchId) -> CaptureBatchDashboardView:
        with self._reader() as connection:
            row = connection.execute(
                sql.EXACT_BATCH_SQL, {"batch_id": str(batch_id)}
            ).fetchone()
            if row is None:
                raise DashboardNotFound(f"capture batch {batch_id} was not found")
            attempts = _attempts_by_sequence(connection, (row,))
        return _batch(row, attempts)

    @contextmanager
    def _reader(self) -> Iterator[psycopg.Connection[dict[str, object]]]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            state = connection.execute("SHOW transaction_read_only").fetchone()
            if state is None or state["transaction_read_only"] != "on":
                raise RuntimeError("dashboard transaction is not read-only")
            yield connection


def _attempts_by_sequence(
    connection: psycopg.Connection[dict[str, object]],
    batches: Sequence[dict[str, object]],
) -> dict[int, tuple[CaptureAttemptDashboardView, ...]]:
    sequences = [
        _int(row["projection_sequence"], "projection_sequence") for row in batches
    ]
    if not sequences:
        return {}
    rows = connection.execute(
        sql.BATCH_ATTEMPTS_SQL, {"projection_sequences": sequences}
    ).fetchall()
    grouped: dict[int, list[CaptureAttemptDashboardView]] = {}
    for row in rows:
        grouped.setdefault(
            _int(row["projection_sequence"], "projection_sequence"), []
        ).append(_attempt(row))
    return {key: tuple(value) for key, value in grouped.items()}


def _attempt(row: dict[str, object]) -> CaptureAttemptDashboardView:
    return CaptureAttemptDashboardView(
        CaptureAttemptId(str(row["attempt_id"])),
        RadioId(str(row["radio_id"])),
        PlanId(str(row["plan_id"])),
        UtcNs(_int(row["requested_start_utc_ns"], "requested_start_utc_ns")),
        DashboardCaptureState(str(row["capture_state"])),
        _optional_utc_ns(row["observed_start_utc_ns"], "observed_start_utc_ns"),
        None if row["recording_id"] is None else RecordingId(str(row["recording_id"])),
        None if row["failure_reason"] is None else str(row["failure_reason"]),
        DashboardAnalysisState(str(row["analysis_state"])),
        _bool(row["analysis_result_available"], "analysis_result_available"),
    )


def _view_from_document(value: object) -> CaptureBatchDashboardView:
    document = _document_mapping(value, "capture batch")
    schema = _document_mapping(document.get("schema"), "schema")
    version = _document_mapping(schema.get("version"), "schema version")
    raw_attempts = document.get("attempts")
    if not isinstance(raw_attempts, list) or len(raw_attempts) != 2:
        raise TypeError("database capture batch attempts are invalid")
    attempts = tuple(
        _attempt_from_document(_document_mapping(item, "capture attempt"))
        for item in raw_attempts
    )
    return CaptureBatchDashboardView(
        SchemaRef(
            _document_string(schema.get("schema_id"), "schema_id"),
            SchemaVersion(
                _int(version.get("major"), "schema major"),
                _int(version.get("minor"), "schema minor"),
            ),
        ),
        CaptureBatchId(_document_string(document.get("batch_id"), "batch_id")),
        CaptureBatchMode(_document_string(document.get("mode"), "mode")),
        CoordinationClaim(
            _document_string(document.get("coordination_claim"), "coordination_claim")
        ),
        cast(tuple[CaptureAttemptDashboardView, CaptureAttemptDashboardView], attempts),
        _int(document.get("revision"), "revision"),
        _int(document.get("requested_start_skew_ns"), "requested_start_skew_ns"),
        _optional_int(document.get("observed_start_skew_ns"), "observed_start_skew_ns"),
        _optional_int(
            document.get("maximum_observed_start_skew_ns"),
            "maximum_observed_start_skew_ns",
        ),
        PairedAnalysisEligibility(
            _document_string(
                document.get("paired_analysis_eligibility"),
                "paired_analysis_eligibility",
            )
        ),
    )


def _attempt_from_document(
    document: dict[str, object],
) -> CaptureAttemptDashboardView:
    return CaptureAttemptDashboardView(
        CaptureAttemptId(_document_string(document.get("attempt_id"), "attempt_id")),
        RadioId(_document_string(document.get("radio_id"), "radio_id")),
        PlanId(_document_string(document.get("plan_id"), "plan_id")),
        UtcNs(_int(document.get("requested_start_utc_ns"), "requested_start_utc_ns")),
        DashboardCaptureState(
            _document_string(document.get("capture_state"), "capture_state")
        ),
        _optional_utc_ns(
            document.get("observed_start_utc_ns"), "observed_start_utc_ns"
        ),
        None
        if document.get("recording_id") is None
        else RecordingId(
            _document_string(document.get("recording_id"), "recording_id")
        ),
        None
        if document.get("failure_reason") is None
        else _document_string(document.get("failure_reason"), "failure_reason"),
        DashboardAnalysisState(
            _document_string(document.get("analysis_state"), "analysis_state")
        ),
        _bool(
            document.get("analysis_result_available"),
            "analysis_result_available",
        ),
    )


def _document_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"database {field} is not an object")
    return cast(dict[str, object], value)


def _document_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"database {field} is not a string")
    return value


def _batch(
    row: dict[str, object],
    attempts: dict[int, tuple[CaptureAttemptDashboardView, ...]],
) -> CaptureBatchDashboardView:
    sequence = _int(row["projection_sequence"], "projection_sequence")
    items = attempts.get(sequence, ())
    if len(items) != 2:
        raise RuntimeError("capture batch projection has incomplete attempts")
    return CaptureBatchDashboardView(
        SchemaRef(
            str(row["schema_id"]), SchemaVersion.parse(str(row["schema_version"]))
        ),
        CaptureBatchId(str(row["batch_id"])),
        CaptureBatchMode(str(row["mode"])),
        CoordinationClaim(str(row["coordination_claim"])),
        items,
        _int(row["capture_revision"], "capture_revision"),
        _int(row["requested_start_skew_ns"], "requested_start_skew_ns"),
        _optional_int(row["observed_start_skew_ns"], "observed_start_skew_ns"),
        _optional_int(
            row["maximum_observed_start_skew_ns"],
            "maximum_observed_start_skew_ns",
        ),
        PairedAnalysisEligibility(str(row["paired_analysis_eligibility"])),
    )


def _anchor(connection: psycopg.Connection[dict[str, object]]) -> int:
    row = connection.execute(sql.BATCH_PROJECTION_ANCHOR_SQL).fetchone()
    if row is None:
        return -1
    return _int(row["projection_sequence"], "projection_sequence")


def _encode_cursor(query: str, anchor: int, after: list[object]) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": "capture_batches",
            "query": query,
            "anchor": anchor,
            "after": after,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None, query: str) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        state = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        )
        after = state["after"]
        if (
            set(state) != {"v", "kind", "query", "anchor", "after"}
            or state["v"] != _CURSOR_VERSION
            or state["kind"] != "capture_batches"
            or state["query"] != query
            or isinstance(state["anchor"], bool)
            or not isinstance(state["anchor"], int)
            or not isinstance(after, list)
            or len(after) != 2
            or isinstance(after[0], bool)
            or not isinstance(after[0], int)
            or not isinstance(after[1], str)
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


def _optional_utc_ns(value: object, field: str) -> UtcNs | None:
    return None if value is None else UtcNs(_int(value, field))


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"database {field} is not a boolean")
    return value
