"""Read-only bounded detector-score histogram query."""

SCORE_DISTRIBUTIONS_SQL = """
WITH latest_recording AS (
    SELECT DISTINCT ON (recording_id)
           recording_id, radio_id, started_utc_ns
      FROM public.dashboard_recording_projection
     WHERE started_utc_ns >= %(start_utc_ns)s
       AND started_utc_ns < %(stop_utc_ns)s
       AND (%(radio_ids)s::text[] = ARRAY[]::text[]
            OR radio_id = ANY(%(radio_ids)s::text[]))
     ORDER BY recording_id, projection_sequence DESC
), latest_suite AS (
    SELECT DISTINCT ON (recording_id) recording_id, semantic_view
      FROM public.dashboard_recording_starlink_detector_suite_projection
     ORDER BY recording_id, projection_sequence DESC
), method_scores AS (
    SELECT recording.recording_id,
           method->>'method' AS method,
           (method->>'score')::double precision AS score
      FROM latest_recording AS recording
      JOIN latest_suite AS suite USING (recording_id)
      CROSS JOIN LATERAL jsonb_array_elements(suite.semantic_view->'methods') AS method
     WHERE suite.semantic_view->>'state' = 'candidates'
       AND jsonb_typeof(method->'method') = 'string'
       AND jsonb_typeof(method->'score') = 'number'
), binned AS (
    SELECT method,
           LEAST(floor(score * %(bin_count)s)::integer,
                 %(bin_count)s - 1) AS bin_index,
           count(*) AS bin_count
      FROM method_scores
     GROUP BY method, bin_index
), summaries AS (
    SELECT method,
           count(DISTINCT recording_id) AS recording_count,
           count(*) AS score_count,
           avg(score) AS mean,
           stddev_pop(score) AS standard_deviation,
           min(score) AS minimum,
           max(score) AS maximum
      FROM method_scores
     GROUP BY method
)
SELECT summary.method, summary.recording_count, summary.score_count,
       summary.mean, summary.standard_deviation, summary.minimum, summary.maximum,
       coalesce(
           jsonb_object_agg(binned.bin_index::text, binned.bin_count)
               FILTER (WHERE binned.bin_index IS NOT NULL),
           '{}'::jsonb
       ) AS bins
  FROM summaries AS summary
  LEFT JOIN binned USING (method)
 GROUP BY summary.method, summary.recording_count, summary.score_count,
          summary.mean, summary.standard_deviation, summary.minimum, summary.maximum
 ORDER BY summary.method
"""
