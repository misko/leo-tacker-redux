"""Read-only query for the additive observation aggregate projection."""

OBSERVATION_ROWS_SQL = """
WITH latest_recording AS (
    SELECT DISTINCT ON (recording_id)
           recording_id, radio_id, started_utc_ns
      FROM public.dashboard_recording_projection
     WHERE started_utc_ns >= %(start_utc_ns)s
       AND started_utc_ns < %(stop_utc_ns)s
       AND (%(radio_ids)s::text[] = ARRAY[]::text[]
            OR radio_id = ANY(%(radio_ids)s::text[]))
     ORDER BY recording_id, projection_sequence DESC
), latest_detail AS (
    SELECT DISTINCT ON (recording_id) recording_id, semantic_view
      FROM public.dashboard_recording_detail_projection
     ORDER BY recording_id, projection_sequence DESC
), latest_suite AS (
    SELECT DISTINCT ON (recording_id) recording_id, semantic_view
      FROM public.dashboard_recording_starlink_detector_suite_projection
     ORDER BY recording_id, projection_sequence DESC
)
SELECT recording.recording_id, recording.radio_id,
       detail.semantic_view AS capture_view,
       suite.semantic_view AS suite_view
  FROM latest_recording AS recording
  JOIN latest_detail AS detail USING (recording_id)
  LEFT JOIN latest_suite AS suite USING (recording_id)
 ORDER BY recording.started_utc_ns DESC, recording.recording_id DESC
"""
