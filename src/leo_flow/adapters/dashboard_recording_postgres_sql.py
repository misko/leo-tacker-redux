"""Statements for recording-detail and waterfall dashboard projections."""

PUBLISH_RECORDING_DETAIL_SQL = """
SELECT public.publish_dashboard_recording_detail(%(view)s::jsonb)
       AS projection_sequence
"""

PUBLISH_RECORDING_WATERFALL_SQL = """
SELECT public.publish_dashboard_recording_waterfall(%(view)s::jsonb)
       AS projection_sequence
"""

PUBLISH_RECORDING_STARLINK_SQL = """
SELECT public.publish_dashboard_recording_starlink(
         %(view)s::jsonb, %(work_id)s, %(lease_token)s, %(lease_generation)s
       )
       AS projection_sequence
"""

EXACT_RECORDING_DETAIL_SQL = """
SELECT detail.semantic_view,
       recording.analysis_state,
       recording.recording_object_available
  FROM public.dashboard_recording_detail_projection AS detail
  JOIN LATERAL (
       SELECT analysis_state, recording_object_available
         FROM public.dashboard_recording_projection
        WHERE recording_id = detail.recording_id
        ORDER BY projection_sequence DESC
        LIMIT 1
  ) AS recording ON TRUE
 WHERE detail.recording_id = %(recording_id)s
"""

EXACT_RECORDING_WATERFALL_SQL = """
SELECT semantic_view
  FROM public.dashboard_recording_waterfall_projection
 WHERE recording_id = %(recording_id)s
 ORDER BY projection_sequence DESC
 LIMIT 1
"""

EXACT_RECORDING_STARLINK_SQL = """
SELECT semantic_view
  FROM public.dashboard_recording_starlink_projection
 WHERE recording_id = %(recording_id)s
 ORDER BY projection_sequence DESC
 LIMIT 1
"""

PUBLISH_RECORDING_STARLINK_SUITE_SQL = """
SELECT public.publish_dashboard_recording_starlink_detector_suite(
    %(view)s, %(work_id)s, %(lease_token)s, %(lease_generation)s
) AS projection_sequence
"""

EXACT_RECORDING_STARLINK_SUITE_SQL = """
SELECT semantic_view
FROM public.dashboard_recording_starlink_detector_suite_projection
WHERE recording_id = %(recording_id)s
ORDER BY projection_sequence DESC
LIMIT 1
"""
