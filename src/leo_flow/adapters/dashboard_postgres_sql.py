"""Parameterized PostgreSQL statements for the dashboard read model."""

PROJECTION_ANCHOR_SQL = """
SELECT last_value
FROM dashboard_projection_sequence
"""

RECENT_RECORDINGS_SQL = """
WITH latest_recording AS (
    SELECT DISTINCT ON (recording_id)
           recording_id, radio_id, started_utc_ns, finished_utc_ns,
           analysis_state, segment_count, recording_object_available
    FROM dashboard_recording_projection
    WHERE projection_sequence <= %(anchor)s
    ORDER BY recording_id, projection_sequence DESC
), latest_activity AS (
    SELECT DISTINCT ON (activity_id)
           activity_id, recording_id, kind
    FROM dashboard_activity_projection
    WHERE projection_sequence <= %(anchor)s
    ORDER BY activity_id, projection_sequence DESC
), activity_kinds AS (
    SELECT recording_id, array_agg(DISTINCT kind ORDER BY kind) AS kinds
    FROM latest_activity
    GROUP BY recording_id
)
SELECT recording.recording_id, recording.radio_id,
       recording.started_utc_ns, recording.finished_utc_ns,
       recording.analysis_state, recording.segment_count,
       recording.recording_object_available,
       COALESCE(activity.kinds, ARRAY[]::text[]) AS activity_kinds
FROM latest_recording AS recording
LEFT JOIN activity_kinds AS activity USING (recording_id)
WHERE recording.started_utc_ns >= %(start_utc_ns)s
  AND recording.started_utc_ns < %(stop_utc_ns)s
  AND (%(radio_ids)s::text[] = ARRAY[]::text[]
       OR recording.radio_id = ANY(%(radio_ids)s::text[]))
  AND (%(after_started)s::bigint IS NULL
       OR (recording.started_utc_ns, recording.recording_id)
          < (%(after_started)s::bigint, %(after_id)s::text))
ORDER BY recording.started_utc_ns DESC, recording.recording_id DESC
LIMIT %(limit)s
"""

ACTIVITY_SQL = """
WITH latest_activity AS (
    SELECT DISTINCT ON (activity_id)
           activity_id, radio_id, kind, started_utc_ns
    FROM dashboard_activity_projection
    ORDER BY activity_id, projection_sequence DESC
)
SELECT radio_id, kind, count(*) AS activity_count
FROM latest_activity
WHERE started_utc_ns >= %(start_utc_ns)s
  AND started_utc_ns < %(stop_utc_ns)s
  AND (%(radio_ids)s::text[] = ARRAY[]::text[]
       OR radio_id = ANY(%(radio_ids)s::text[]))
GROUP BY radio_id, kind
ORDER BY radio_id, kind
"""

RECORDING_DETAIL_SQL = """
WITH latest_recording AS (
    SELECT DISTINCT ON (recording_id)
           recording_id, radio_id, started_utc_ns, finished_utc_ns,
           analysis_state, segment_count, recording_object_available
    FROM dashboard_recording_projection
    WHERE recording_id = %(recording_id)s
    ORDER BY recording_id, projection_sequence DESC
), latest_activity AS (
    SELECT DISTINCT ON (activity_id)
           activity_id, recording_id, kind
    FROM dashboard_activity_projection
    WHERE recording_id = %(recording_id)s
    ORDER BY activity_id, projection_sequence DESC
)
SELECT recording.recording_id, recording.radio_id,
       recording.started_utc_ns, recording.finished_utc_ns,
       recording.analysis_state, recording.segment_count,
       recording.recording_object_available,
       COALESCE(array_agg(DISTINCT activity.kind ORDER BY activity.kind)
                FILTER (WHERE activity.kind IS NOT NULL),
                ARRAY[]::text[]) AS activity_kinds
FROM latest_recording AS recording
LEFT JOIN latest_activity AS activity USING (recording_id)
GROUP BY recording.recording_id, recording.radio_id,
         recording.started_utc_ns, recording.finished_utc_ns,
         recording.analysis_state, recording.segment_count,
         recording.recording_object_available
"""

FEATURES_SQL = """
WITH latest_feature AS (
    SELECT DISTINCT ON (feature_id)
           feature_id, method_id, score, score_semantics
    FROM dashboard_feature_projection
    WHERE recording_id = %(recording_id)s
      AND projection_sequence <= %(anchor)s
    ORDER BY feature_id, projection_sequence DESC
)
SELECT feature_id, method_id, score, score_semantics
FROM latest_feature
WHERE (%(selector)s = '*' OR method_id = %(selector)s)
  AND (%(after_id)s::text IS NULL OR feature_id > %(after_id)s::text)
ORDER BY feature_id
LIMIT %(limit)s
"""

MODEL_SQL = """
WITH latest_model AS (
    SELECT DISTINCT ON (model_snapshot_id)
           model_snapshot_id, release_alias, parameter_count, warnings,
           projection_sequence
    FROM dashboard_model_projection
    ORDER BY model_snapshot_id, projection_sequence DESC
)
SELECT model_snapshot_id, release_alias, parameter_count, warnings
FROM latest_model
WHERE model_snapshot_id = %(identity)s OR release_alias = %(identity)s
ORDER BY projection_sequence DESC
"""

TRACKS_SQL = """
WITH latest_track AS (
    SELECT DISTINCT ON (track_id)
           track_id, model_snapshot_id, radio_id,
           started_utc_ns, finished_utc_ns
    FROM dashboard_track_projection
    WHERE projection_sequence <= %(anchor)s
    ORDER BY track_id, projection_sequence DESC
)
SELECT track_id, model_snapshot_id, started_utc_ns, finished_utc_ns
FROM latest_track
WHERE started_utc_ns >= %(start_utc_ns)s
  AND started_utc_ns < %(stop_utc_ns)s
  AND (%(radio_ids)s::text[] = ARRAY[]::text[]
       OR radio_id = ANY(%(radio_ids)s::text[]))
  AND (%(after_started)s::bigint IS NULL
       OR (started_utc_ns, track_id)
          < (%(after_started)s::bigint, %(after_id)s::text))
ORDER BY started_utc_ns DESC, track_id DESC
LIMIT %(limit)s
"""

STORAGE_HEALTH_SQL = """
SELECT available, total_bytes, free_bytes
FROM dashboard_storage_health_projection
ORDER BY projection_sequence DESC
LIMIT 1
"""
