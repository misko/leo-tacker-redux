"""Driver-neutral fenced PostgreSQL lease statements.

These statements require transaction tests against PostgreSQL once a driver and
test service are approved.  The in-memory adapter is not a substitute for them.
"""

CLAIM_SQL = """
WITH candidate AS (
    SELECT job_id
    FROM job
    WHERE job_type = ANY(%(job_types)s)
      AND available_at_utc <= clock_timestamp()
      AND (
          state IN ('ready', 'failed')
          OR (state = 'leased' AND lease_expires_utc <= clock_timestamp())
      )
    ORDER BY available_at_utc, job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE job AS j
SET state = 'leased',
    attempt = j.attempt + 1,
    lease_generation = j.lease_generation + 1,
    lease_token = %(lease_token)s,
    lease_expires_utc = clock_timestamp() + %(ttl_interval)s
FROM candidate
WHERE j.job_id = candidate.job_id
RETURNING j.*
"""

HEARTBEAT_SQL = """
UPDATE job
SET lease_expires_utc = clock_timestamp() + %(ttl_interval)s
WHERE job_id = %(job_id)s
  AND state = 'leased'
  AND lease_token = %(lease_token)s
  AND lease_generation = %(lease_generation)s
  AND lease_expires_utc > clock_timestamp()
RETURNING *
"""

COMPLETE_SQL = """
UPDATE job
SET state = 'succeeded', result_ref = %(result_ref)s,
    lease_token = NULL, lease_expires_utc = NULL
WHERE job_id = %(job_id)s
  AND state = 'leased'
  AND lease_token = %(lease_token)s
  AND lease_generation = %(lease_generation)s
  AND lease_expires_utc > clock_timestamp()
RETURNING job_id
"""

FAIL_SQL = """
UPDATE job
SET state = 'failed', last_error = %(reason)s,
    available_at_utc = %(retry_at_utc)s,
    lease_token = NULL, lease_expires_utc = NULL
WHERE job_id = %(job_id)s
  AND state = 'leased'
  AND lease_token = %(lease_token)s
  AND lease_generation = %(lease_generation)s
  AND lease_expires_utc > clock_timestamp()
RETURNING job_id
"""
