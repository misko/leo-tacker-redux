from __future__ import annotations

import psycopg
import pytest


@pytest.mark.integration
def test_qam_summary_receipt_is_private_terminal_and_directionally_scoped(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT to_regclass(
                     'public.dashboard_capture_qam_summary_receipt_v0_2')::text,
                   has_table_privilege(
                     'leo_analysis','dashboard_capture_qam_summary_receipt_v0_2','SELECT'),
                   has_table_privilege(
                     'leo_dashboard','dashboard_capture_qam_summary_receipt_v0_2','SELECT'),
                   has_table_privilege(
                     'leo_capture','dashboard_capture_qam_summary_receipt_v0_2','SELECT'),
                   has_function_privilege(
                     'leo_analysis',
                     'publish_dashboard_capture_qam_summary_receipt_v0_2(text,text,text,text,text,jsonb)',
                     'EXECUTE'),
                   has_function_privilege(
                     'leo_dashboard',
                     'publish_dashboard_capture_qam_summary_receipt_v0_2(text,text,text,text,text,jsonb)',
                     'EXECUTE'),
                   has_function_privilege(
                     'leo_capture',
                     'publish_dashboard_capture_qam_summary_receipt_v0_2(text,text,text,text,text,jsonb)',
                     'EXECUTE'),
                   has_function_privilege(
                     'leo_analysis',
                     'read_pending_dashboard_capture_qam_products_v0_2(integer)',
                     'EXECUTE'),
                   has_function_privilege(
                     'leo_dashboard',
                     'read_pending_dashboard_capture_qam_products_v0_2(integer)',
                     'EXECUTE')
            """
        ).fetchone()
        pending_definition = connection.execute(
            "SELECT pg_get_functiondef("
            "'read_pending_dashboard_capture_qam_products_v0_2(integer)'::regprocedure)"
        ).fetchone()

    assert row == (
        "dashboard_capture_qam_summary_receipt_v0_2",
        False,
        False,
        False,
        True,
        False,
        False,
        True,
        False,
    )
    assert pending_definition is not None
    assert "LIMIT $1" in str(pending_definition[0])
    assert "dashboard_capture_qam_summary_receipt_v0_2" in str(
        pending_definition[0]
    )
