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


POINT_SCORE_DISTRIBUTIONS_SQL = """
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
), point_rows AS (
    SELECT recording.recording_id,
           recording.radio_id,
           method->>'segment_id' AS segment_id,
           method->>'receiver_chain_id' AS receiver_chain_id,
           method->>'edge' AS edge,
           method->>'method' AS method,
           score.score_kind,
           score.value
      FROM latest_recording AS recording
      JOIN latest_suite AS suite USING (recording_id)
      CROSS JOIN LATERAL jsonb_array_elements(suite.semantic_view->'methods') AS method
      CROSS JOIN LATERAL (
          VALUES ('candidate', (method->>'score')::double precision),
                 ('conditioned-control', (method->>'control_score')::double precision)
      ) AS score(score_kind, value)
     WHERE suite.semantic_view->>'state' = 'candidates'
       AND jsonb_typeof(method->'segment_id') = 'string'
       AND jsonb_typeof(method->'receiver_chain_id') = 'string'
       AND jsonb_typeof(method->'edge') = 'string'
       AND jsonb_typeof(method->'method') = 'string'
       AND jsonb_typeof(method->'score') = 'number'
       AND jsonb_typeof(method->'control_score') = 'number'
), binned AS (
    SELECT method, radio_id, receiver_chain_id, edge, score_kind,
           LEAST(floor(value * %(bin_count)s)::integer,
                 %(bin_count)s - 1) AS bin_index,
           count(*) AS bin_count
      FROM point_rows
     GROUP BY method, radio_id, receiver_chain_id, edge, score_kind, bin_index
), summaries AS (
    SELECT method, radio_id, receiver_chain_id, edge, score_kind,
           count(*) AS source_row_count,
           count(DISTINCT (recording_id, segment_id, radio_id,
                           receiver_chain_id, edge, method)) AS point_count,
           count(DISTINCT recording_id) AS recording_count,
           avg(value) AS mean,
           stddev_pop(value) AS standard_deviation,
           min(value) AS minimum,
           max(value) AS maximum
      FROM point_rows
     GROUP BY method, radio_id, receiver_chain_id, edge, score_kind
)
SELECT summary.method, summary.radio_id, summary.receiver_chain_id,
       summary.edge, summary.score_kind, summary.source_row_count,
       summary.point_count, summary.recording_count, summary.mean,
       summary.standard_deviation, summary.minimum, summary.maximum,
       coalesce(
           jsonb_object_agg(binned.bin_index::text, binned.bin_count)
               FILTER (WHERE binned.bin_index IS NOT NULL),
           '{}'::jsonb
       ) AS bins
  FROM summaries AS summary
  LEFT JOIN binned USING (method, radio_id, receiver_chain_id, edge, score_kind)
 GROUP BY summary.method, summary.radio_id, summary.receiver_chain_id,
          summary.edge, summary.score_kind, summary.source_row_count,
          summary.point_count, summary.recording_count, summary.mean,
          summary.standard_deviation, summary.minimum, summary.maximum
 ORDER BY summary.method, summary.radio_id, summary.receiver_chain_id,
          summary.edge, summary.score_kind
"""
