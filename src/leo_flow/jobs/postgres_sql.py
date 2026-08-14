"""Driver-neutral calls into fenced PostgreSQL job transition functions."""

ENQUEUE_SQL = """
SELECT enqueue_job(
    %(job_id)s, %(job_type)s, %(payload_schema_id)s,
    %(payload_schema_version)s, %(payload)s, %(available_at_utc)s)
"""

CLAIM_SQL = """
SELECT * FROM claim_job(
    %(job_types)s, %(lease_token)s, %(ttl_interval)s)
"""

HEARTBEAT_SQL = """
SELECT * FROM heartbeat_job(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(ttl_interval)s)
"""

LOCK_ACTIVE_SQL = """
SELECT true AS active
WHERE lock_active_job_lease(
    %(job_id)s, %(job_type)s, %(lease_token)s, %(lease_generation)s)
"""

COMPLETE_SQL = """
SELECT * FROM complete_job(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(result_ref)s)
"""

FAIL_SQL = """
SELECT * FROM fail_job(
    %(job_id)s, %(lease_token)s, %(lease_generation)s,
    %(reason)s, %(retry_at_utc)s)
"""

PARK_SQL = """
SELECT * FROM park_job(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(reason)s)
"""

SNAPSHOT_SQL = """
SELECT * FROM job WHERE job_id = %(job_id)s
"""
