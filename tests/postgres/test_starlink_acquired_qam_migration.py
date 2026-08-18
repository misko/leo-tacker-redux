from __future__ import annotations

import psycopg
import pytest


@pytest.mark.integration
def test_acquired_qam_catalog_is_additive_bounded_and_role_scoped(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT to_regclass('public.recording_starlink_acquired_constellation_v0_3')::text,
                   has_table_privilege('leo_analysis','recording_starlink_acquired_constellation_v0_3','INSERT'),
                   has_table_privilege('leo_dashboard','recording_starlink_acquired_constellation_v0_3','SELECT'),
                   has_table_privilege('leo_dashboard','recording_starlink_acquired_constellation_v0_3','INSERT'),
                   has_function_privilege('leo_analysis','publish_recording_starlink_acquired_constellation_v0_3(jsonb)','EXECUTE'),
                   has_function_privilege('leo_dashboard','read_latest_recording_starlink_acquired_constellation_v0_3(text)','EXECUTE')
            """
        ).fetchone()
        live_reference_definition = connection.execute(
            "SELECT pg_get_viewdef('public.object_blob_live_reference'::regclass,true)"
        ).fetchone()
    assert row == (
        "recording_starlink_acquired_constellation_v0_3",
        False,
        False,
        False,
        True,
        True,
    )
    assert live_reference_definition is not None
    assert "recording_starlink_acquired_constellation_v0_3" in str(
        live_reference_definition[0]
    )
