BEGIN;

-- Deferred collection may leave recording-analysis jobs and feature
-- projections pending.  Capture remains admissible only while neither queue
-- has a current live lease.  Expired leases are recoverable pending work, not
-- evidence of an active worker.
CREATE FUNCTION public.capture_analysis_inactive()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
               SELECT 1
                 FROM public.job AS active_job
                WHERE active_job.state = 'leased'
                  AND active_job.job_type IN (
                      'recording_analysis', 'model_analysis'
                  )
                  AND active_job.lease_expires_utc
                      > pg_catalog.clock_timestamp()
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.feature_projection_work AS active_projection
                WHERE active_projection.state = 'leased'
                  AND active_projection.lease_expires_utc
                      > pg_catalog.clock_timestamp()
           );
$function$;

ALTER FUNCTION public.capture_analysis_inactive()
    OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.capture_analysis_inactive()
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION public.capture_analysis_inactive()
TO leo_capture;

COMMIT;
