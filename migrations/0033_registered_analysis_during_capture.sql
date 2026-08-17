BEGIN;

-- A capture process may overlap any analysis lease whose source job belongs
-- to an exact terminal 36-batch scope registered by migration 0032.  The
-- caller's definition digest remains a mandatory, canonical admission
-- identity, but analysis no longer has to originate from that same campaign.
-- This permits bounded historical backfill without pausing synchronized RF
-- capture while still rejecting legacy, unregistered, or unrelated work.
CREATE FUNCTION public.capture_registered_analysis_safe_v2(
    p_capture_definition_digest text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    SELECT p_capture_definition_digest ~ '^sha256:[0-9a-f]{64}$'
       AND NOT EXISTS (
           SELECT 1 FROM public.job AS j
            WHERE j.state='leased'
              AND j.job_type IN (
                  'recording_analysis','model_analysis','waterfall_analysis',
                  'starlink_analysis','starlink_suite_analysis')
              AND j.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=j.job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.feature_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.waterfall_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp())
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_detector_suite_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id));
$function$;

ALTER FUNCTION public.capture_registered_analysis_safe_v2(text)
    OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.capture_registered_analysis_safe_v2(text)
    FROM PUBLIC,leo_analysis,leo_capture,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.capture_registered_analysis_safe_v2(text)
    TO leo_capture;

COMMIT;
