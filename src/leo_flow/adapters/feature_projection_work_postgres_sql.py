"""Static calls into the dedicated PostgreSQL feature-projection work API."""

PUBLISH_SQL = """
SELECT public.publish_feature_projection_work(
    %(work_id)s, %(source_job_id)s, %(source_lease_token)s,
    %(source_lease_generation)s, %(feature_set_id)s, %(analysis_run_id)s,
    %(feature_digest_algorithm)s, %(feature_digest_value)s,
    %(recording_id)s, %(recording_digest_algorithm)s,
    %(recording_digest_value)s)
"""

CLAIM_SQL = """
SELECT * FROM public.claim_feature_projection_work(
    %(lease_token)s, %(ttl_interval)s)
"""

CLAIM_SCOPED_SQL = """
SELECT * FROM public.claim_campaign_feature_projection(
    %(source_job_ids)s, %(lease_token)s, %(ttl_interval)s)
"""

HEARTBEAT_SQL = """
SELECT * FROM public.heartbeat_feature_projection_work(
    %(work_id)s, %(lease_token)s, %(lease_generation)s, %(ttl_interval)s)
"""

COMPLETE_SQL = """
SELECT public.complete_feature_projection_work(
    %(work_id)s, %(lease_token)s, %(lease_generation)s)
"""

RETRY_SQL = """
SELECT public.retry_feature_projection_work(
    %(work_id)s, %(lease_token)s, %(lease_generation)s,
    %(reason)s, %(delay_interval)s)
"""

PARK_SQL = """
SELECT public.park_feature_projection_work(
    %(work_id)s, %(lease_token)s, %(lease_generation)s, %(reason)s)
"""
