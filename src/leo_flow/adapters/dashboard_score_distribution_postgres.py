"""PostgreSQL adapter for bounded detector-score distributions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import psycopg

from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_score_distribution import (
    SCORE_HISTOGRAM_BIN_COUNT,
    MethodScoreDistributionV0_1,
    PointScoreDistributionV0_2,
    PointScoreDistributionViewV0_2,
    ScoreDistributionViewV0_1,
    ScoreHistogramBinV0_1,
)

from .dashboard_score_distribution_postgres_sql import (
    POINT_SCORE_DISTRIBUTIONS_SQL,
    SCORE_DISTRIBUTIONS_SQL,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresScoreDistributionRepositoryV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def score_distributions(self, query: TimeRangeQuery) -> ScoreDistributionViewV0_1:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                SCORE_DISTRIBUTIONS_SQL,
                {
                    "start_utc_ns": int(query.start_utc_ns),
                    "stop_utc_ns": int(query.stop_utc_ns),
                    "radio_ids": [str(item) for item in query.radio_ids],
                    "bin_count": SCORE_HISTOGRAM_BIN_COUNT,
                },
            ).fetchall()
        return score_distribution_view_v0_1(query, rows)

    def point_score_distributions(
        self, query: TimeRangeQuery
    ) -> PointScoreDistributionViewV0_2:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                POINT_SCORE_DISTRIBUTIONS_SQL,
                {
                    "start_utc_ns": int(query.start_utc_ns),
                    "stop_utc_ns": int(query.stop_utc_ns),
                    "radio_ids": [str(item) for item in query.radio_ids],
                    "bin_count": SCORE_HISTOGRAM_BIN_COUNT,
                },
            ).fetchall()
        return point_score_distribution_view_v0_2(query, rows)


def score_distribution_view_v0_1(
    query: TimeRangeQuery, rows: Sequence[Mapping[str, object]]
) -> ScoreDistributionViewV0_1:
    width = 1.0 / SCORE_HISTOGRAM_BIN_COUNT
    distributions: list[MethodScoreDistributionV0_1] = []
    for row in rows:
        score_count = _integer(row["score_count"])
        raw_bins = row["bins"]
        if not isinstance(raw_bins, Mapping):
            raise TypeError("score histogram bins must be an object")
        bins = tuple(
            ScoreHistogramBinV0_1(
                index,
                index * width,
                (index + 1) * width,
                count := _integer(raw_bins.get(str(index), 0)),
                count / score_count / width,
            )
            for index in range(SCORE_HISTOGRAM_BIN_COUNT)
        )
        distributions.append(
            MethodScoreDistributionV0_1(
                str(row["method"]),
                _integer(row["recording_count"]),
                score_count,
                _number(row["mean"]),
                _number(row["standard_deviation"]),
                _number(row["minimum"]),
                _number(row["maximum"]),
                bins,
            )
        )
    return ScoreDistributionViewV0_1(
        1,
        query.start_utc_ns,
        query.stop_utc_ns,
        0.0,
        1.0,
        SCORE_HISTOGRAM_BIN_COUNT,
        "candidate-method-score-density",
        tuple(distributions),
    )


def point_score_distribution_view_v0_2(
    query: TimeRangeQuery, rows: Sequence[Mapping[str, object]]
) -> PointScoreDistributionViewV0_2:
    width = 1.0 / SCORE_HISTOGRAM_BIN_COUNT
    distributions: list[PointScoreDistributionV0_2] = []
    for row in rows:
        point_count = _integer(row["point_count"])
        if _integer(row["source_row_count"]) != point_count:
            raise ValueError("detector score point identity is duplicated")
        raw_bins = row["bins"]
        if not isinstance(raw_bins, Mapping):
            raise TypeError("point score histogram bins must be an object")
        bins = tuple(
            ScoreHistogramBinV0_1(
                index,
                index * width,
                (index + 1) * width,
                count := _integer(raw_bins.get(str(index), 0)),
                count / point_count / width,
            )
            for index in range(SCORE_HISTOGRAM_BIN_COUNT)
        )
        distributions.append(
            PointScoreDistributionV0_2(
                str(row["method"]),
                str(row["radio_id"]),
                str(row["receiver_chain_id"]),
                str(row["edge"]),
                str(row["score_kind"]),
                _integer(row["recording_count"]),
                point_count,
                _number(row["mean"]),
                _number(row["standard_deviation"]),
                _number(row["minimum"]),
                _number(row["maximum"]),
                bins,
            )
        )
    return PointScoreDistributionViewV0_2(
        2,
        query.start_utc_ns,
        query.stop_utc_ns,
        0.0,
        1.0,
        SCORE_HISTOGRAM_BIN_COUNT,
        "recording+segment+radio+receiver-chain+edge+method",
        tuple(distributions),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected number")
    return float(value)
