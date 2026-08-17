"""PostgreSQL adapter for bounded observation and Starlink aggregates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping

import psycopg

from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_observation import (
    DutyCycleAggregateV0_1,
    ObservationAggregateViewV0_1,
    RecordingStarlinkStateV0_1,
    StarlinkEvidenceAggregateV0_1,
)

from .dashboard_observation_postgres_sql import OBSERVATION_ROWS_SQL

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
_MAX_RECORDING_STATES = 10_000


class PostgresObservationAggregateRepositoryV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def observation_aggregate(
        self, query: TimeRangeQuery
    ) -> ObservationAggregateViewV0_1:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                OBSERVATION_ROWS_SQL,
                {
                    "start_utc_ns": int(query.start_utc_ns),
                    "stop_utc_ns": int(query.stop_utc_ns),
                    "radio_ids": [str(item) for item in query.radio_ids],
                },
            ).fetchall()
        return aggregate_observation_rows_v0_1(query, rows)


def aggregate_observation_rows_v0_1(
    query: TimeRangeQuery, rows: Iterable[Mapping[str, object]]
) -> ObservationAggregateViewV0_1:
    materialized = tuple(rows)
    intervals: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    comparisons: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    states: list[RecordingStarlinkStateV0_1] = []
    state_counts = {"candidates": 0, "not_evaluated": 0, "unavailable": 0}

    for row in materialized:
        recording_id = RecordingId(str(row["recording_id"]))
        radio_id = str(row["radio_id"])
        capture = _mapping(row["capture_view"], "capture_view")
        for segment_value in _sequence(capture.get("segments"), "segments"):
            segment = _mapping(segment_value, "segment")
            start = max(int(query.start_utc_ns), _integer(segment["started_utc_ns"]))
            stop = min(int(query.stop_utc_ns), _integer(segment["finished_utc_ns"]))
            if stop <= start:
                continue
            intervals[("radio", radio_id)].append((start, stop))
            for receiver in _sequence(
                segment.get("receiver_chain_ids"), "receiver_chain_ids"
            ):
                intervals[("lnb", str(receiver))].append((start, stop))

        suite_value = row.get("suite_view")
        state = "unavailable"
        if suite_value is not None:
            suite = _mapping(suite_value, "suite_view")
            state = str(suite.get("state"))
            if state not in {"candidates", "not_evaluated"}:
                raise ValueError("unsupported projected Starlink state")
            if suite.get("calibrated_detection_count") is not None:
                raise ValueError("candidate-only projection declares detections")
            for method_value in _sequence(suite.get("methods"), "methods"):
                method = _mapping(method_value, "method")
                positive = _number(method["score"]) > _number(method["control_score"])
                for key in (
                    ("lnb", str(method["receiver_chain_id"])),
                    ("edge", str(method["edge"])),
                    ("method", str(method["method"])),
                ):
                    comparisons[key][0] += 1
                    comparisons[key][1] += int(positive)
        state_counts[state] += 1
        if len(states) < _MAX_RECORDING_STATES:
            states.append(RecordingStarlinkStateV0_1(recording_id, state))

    interval_ns = int(query.stop_utc_ns) - int(query.start_utc_ns)
    duty = tuple(
        DutyCycleAggregateV0_1(
            dimension,
            identity,
            active_ns := _union_duration(ranges),
            interval_ns,
            active_ns / interval_ns,
        )
        for (dimension, identity), ranges in sorted(intervals.items())
    )
    evidence = tuple(
        StarlinkEvidenceAggregateV0_1(
            dimension,
            identity,
            counts[0],
            counts[1],
            None if counts[0] == 0 else counts[1] / counts[0],
            None,
            None,
        )
        for (dimension, identity), counts in sorted(comparisons.items())
    )
    return ObservationAggregateViewV0_1(
        1,
        query.start_utc_ns,
        query.stop_utc_ns,
        len(materialized),
        state_counts["candidates"],
        state_counts["not_evaluated"],
        state_counts["unavailable"],
        "required",
        "whole-search-calibration-required",
        duty,
        evidence,
        tuple(states),
        len(materialized) > _MAX_RECORDING_STATES,
    )


def _union_duration(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end: int | None = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{name} must be an array")
    return tuple(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected number")
    return float(value)
