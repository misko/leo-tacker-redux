"""Driver-neutral PostgreSQL calls for the durable dwell ingress boundary."""

PUBLISH_SQL = """
SELECT public.publish_dwell_request(%(publication)s)
"""

CLAIM_SQL = """
SELECT * FROM public.claim_dwell_request(
    %(station_id)s, %(radio_id)s, %(lease_token)s, %(ttl_interval)s)
"""

HEARTBEAT_SQL = """
SELECT public.heartbeat_dwell_request(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(ttl_interval)s)
AS lease_expires_utc
"""

COMPLETE_SQL = """
SELECT public.complete_dwell_request(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(result_ref)s)
AS completed
"""

FAIL_SQL = """
SELECT public.fail_dwell_request(
    %(job_id)s, %(lease_token)s, %(lease_generation)s,
    %(reason)s, %(retry_at_utc)s)
AS failed
"""

PARK_SQL = """
SELECT public.park_dwell_request(
    %(job_id)s, %(lease_token)s, %(lease_generation)s, %(reason)s)
AS parked
"""
