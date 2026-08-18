from __future__ import annotations

import psycopg
import pytest


@pytest.mark.integration
def test_qam_summary_projection_is_bounded_and_directionally_scoped(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT to_regclass('public.dashboard_capture_qam_candidate_v0_1')::text,
              has_table_privilege('leo_dashboard','dashboard_capture_qam_candidate_v0_1','SELECT'),
              has_table_privilege('leo_analysis','dashboard_capture_qam_candidate_v0_1','INSERT'),
              has_function_privilege('leo_analysis','publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb)','EXECUTE'),
              has_function_privilege('leo_dashboard','publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb)','EXECUTE'),
              has_function_privilege('leo_dashboard','read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer)','EXECUTE'),
              has_function_privilege('leo_analysis','read_pending_dashboard_capture_qam_products_v0_1(integer)','EXECUTE'),
              has_function_privilege('leo_dashboard','read_pending_dashboard_capture_qam_products_v0_1(integer)','EXECUTE'),
              has_function_privilege('leo_capture','read_pending_dashboard_capture_qam_products_v0_1(integer)','EXECUTE')
            """
        ).fetchone()
        definition = connection.execute(
            "SELECT pg_get_functiondef('read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer)'::regprocedure)"
        ).fetchone()
        pending_definition = connection.execute(
            "SELECT pg_get_functiondef('read_pending_dashboard_capture_qam_products_v0_1(integer)'::regprocedure)"
        ).fetchone()
    assert row == (
        "dashboard_capture_qam_candidate_v0_1",
        False,
        False,
        True,
        False,
        True,
        True,
        False,
        False,
    )
    assert definition is not None
    assert "LIMIT $3" in str(definition[0])
    assert "object_blob" not in str(definition[0])
    assert pending_definition is not None
    assert "LIMIT $1" in str(pending_definition[0])
    assert "dashboard_capture_qam_candidate_v0_1" in str(pending_definition[0])
