from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import JobId, SchemaRef
from leo_flow.jobs import JobPayload, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services import (
    EphemerisLinkBackfillUnavailable,
    UnsupportedAnalysisJobError,
)


@pytest.mark.integration
def test_unavailable_backfill_is_durably_failed_without_hot_reclaim(
    postgres_dsn: str,
) -> None:
    jobs = PostgresJobLeaseRepository(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    jobs.enqueue(
        JobId("job_backfill_unavailable"),
        JobType.EPHEMERIS_LINK_BACKFILL,
        JobPayload.create(SchemaRef("backfill-job"), {}),
    )
    lease = jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "router", 5.0)
    assert lease is not None

    with pytest.raises(UnsupportedAnalysisJobError, match="not-implemented"):
        EphemerisLinkBackfillUnavailable(jobs).execute(lease)
    assert jobs.claim((JobType.EPHEMERIS_LINK_BACKFILL,), "router", 5.0) is None
    with psycopg.connect(postgres_dsn) as connection:
        state, reason, available = connection.execute(
            "SELECT state, last_error, extract(year FROM available_at_utc) "
            "FROM job WHERE job_id = %s",
            (str(lease.job_id),),
        ).fetchone()
    assert state == "failed"
    assert reason == EphemerisLinkBackfillUnavailable.REASON
    assert int(available) == 9999
