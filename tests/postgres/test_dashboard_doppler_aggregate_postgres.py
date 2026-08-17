from __future__ import annotations

import psycopg
import pytest


def test_doppler_aggregate_routines_are_dashboard_only_and_read_only(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        dashboard = connection.cursor()
        dashboard.execute("SET ROLE leo_dashboard")
        assert dashboard.execute(
            "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_count_v1(%s,%s)",
            (1, 2),
        ).fetchone() == (0, 0)
        assert (
            dashboard.execute(
                "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_v1(%s,%s,%s)",
                (1, 2, 1),
            ).fetchall()
            == []
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            dashboard.execute("SELECT count(*) FROM public.recording_doppler_analysis")
        connection.rollback()

        capture = connection.cursor()
        capture.execute("SET ROLE leo_capture")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            capture.execute(
                "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_count_v1(1,2)"
            )
        connection.rollback()


def test_doppler_aggregate_routines_reject_unbounded_or_reversed_queries(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        for statement in (
            "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_count_v1(2,1)",
            "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_v1(1,2,514)",
            "SELECT * FROM public.read_dashboard_doppler_aggregate_interval_v1(-1,2,1)",
        ):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                connection.execute(statement)
            connection.rollback()
            connection.execute("SET ROLE leo_dashboard")


def test_doppler_aggregate_query_has_a_bounded_database_plan(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        plan = "\n".join(
            str(row[0])
            for row in connection.execute(
                "EXPLAIN (COSTS OFF) SELECT * FROM "
                "public.read_dashboard_doppler_aggregate_interval_v1(1,2,513)"
            ).fetchall()
        )
    assert "Function Scan" in plan
