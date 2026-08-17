BEGIN;

-- A full capture admission drain includes every retryable recording and
-- waterfall analysis item and both durable projection outboxes.  Parked and
-- succeeded rows are terminal and therefore do not hold the drain closed.
CREATE OR REPLACE FUNCTION public.capture_analysis_drain_ready()
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    is_ready boolean;
BEGIN
    SELECT NOT EXISTS (
               SELECT 1
                 FROM (
                     SELECT DISTINCT ON (projection.recording_id)
                            projection.analysis_state
                       FROM public.dashboard_recording_projection AS projection
                      ORDER BY projection.recording_id,
                               projection.projection_sequence DESC
                 ) AS latest_recording
                WHERE latest_recording.analysis_state IN ('pending', 'running')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.job AS pending_job
                WHERE pending_job.job_type IN (
                          'recording_analysis', 'waterfall_analysis'
                      )
                  AND pending_job.state IN ('ready', 'leased', 'failed')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.feature_projection_work AS pending_projection
                WHERE pending_projection.state IN ('ready', 'leased', 'failed')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.waterfall_projection_work AS pending_waterfall
                WHERE pending_waterfall.state IN ('ready', 'leased', 'failed')
           )
      INTO is_ready;
    RETURN is_ready;
END
$function$;

ALTER FUNCTION public.capture_analysis_drain_ready()
    OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.capture_analysis_drain_ready()
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION public.capture_analysis_drain_ready()
TO leo_capture;

COMMIT;
