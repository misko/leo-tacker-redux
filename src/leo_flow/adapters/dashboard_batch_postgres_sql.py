"""Parameterized statements for the capture-batch dashboard projection."""

PUBLISH_BATCH_SQL = """
SELECT public.publish_dashboard_capture_batch(%(view)s::jsonb)
       AS projection_sequence
"""

RESOLVE_BATCHES_FOR_RECORDING_SQL = """
SELECT batch_id, semantic_view
FROM public.resolve_dashboard_capture_batches_for_recording(%(recording_id)s)
ORDER BY batch_id
"""

BATCH_PROJECTION_ANCHOR_SQL = """
SELECT COALESCE(MAX(projection_sequence), -1) AS projection_sequence
FROM public.dashboard_capture_batch_projection
"""

RECENT_BATCHES_SQL = """
WITH latest_batch AS (
    SELECT DISTINCT ON (batch_id)
           projection_sequence, schema_id, schema_version, batch_id,
           capture_revision, mode, coordination_claim,
           requested_start_utc_ns, requested_start_skew_ns,
           observed_start_skew_ns, maximum_observed_start_skew_ns,
           paired_analysis_eligibility
      FROM public.dashboard_capture_batch_projection
     WHERE projection_sequence <= %(anchor)s
     ORDER BY batch_id, projection_sequence DESC
)
SELECT *
  FROM latest_batch
 WHERE requested_start_utc_ns >= %(start_utc_ns)s
   AND requested_start_utc_ns < %(stop_utc_ns)s
   AND (%(after_started)s::bigint IS NULL
        OR (requested_start_utc_ns, batch_id)
           < (%(after_started)s::bigint, %(after_id)s::text))
 ORDER BY requested_start_utc_ns DESC, batch_id DESC
 LIMIT %(limit)s
"""

EXACT_BATCH_SQL = """
SELECT projection_sequence, schema_id, schema_version, batch_id,
       capture_revision, mode, coordination_claim,
       requested_start_utc_ns, requested_start_skew_ns,
       observed_start_skew_ns, maximum_observed_start_skew_ns,
       paired_analysis_eligibility
  FROM public.dashboard_capture_batch_projection
 WHERE batch_id = %(batch_id)s
 ORDER BY projection_sequence DESC
 LIMIT 1
"""

BATCH_ATTEMPTS_SQL = """
SELECT projection_sequence, attempt_position, attempt_id, radio_id, plan_id,
       requested_start_utc_ns, capture_state, observed_start_utc_ns,
       recording_id, failure_reason, analysis_state,
       analysis_result_available
  FROM public.dashboard_capture_attempt_projection
 WHERE projection_sequence = ANY(%(projection_sequences)s::bigint[])
 ORDER BY projection_sequence, attempt_position
"""
