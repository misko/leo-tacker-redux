BEGIN;

-- Reopen one completed adaptive-response item only when the caller proves the
-- exact result currently attached to it.  Historical products remain
-- immutable; a changed analysis plan publishes a new request/result identity.
CREATE FUNCTION public.requeue_starlink_adaptive_response_work_v0_1(text,text,text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $function$
WITH changed AS (
  UPDATE public.starlink_adaptive_response_work_v0_1 w
  SET state='ready',
      available_at_utc=clock_timestamp(),
      attempt=0,
      result_analysis_id=NULL,
      result_bundle_digest_value=NULL,
      completed_at_utc=NULL,
      last_error=$3
  WHERE w.recording_id=$1
    AND w.state='succeeded'
    AND w.result_analysis_id=$2
    AND $3~'^[a-z0-9][a-z0-9._:-]{0,127}$'
    AND EXISTS (
      SELECT 1
      FROM public.recording_starlink_adaptive_response_v0_1 r
      WHERE r.analysis_id=$2 AND r.recording_id=$1
    )
  RETURNING 1
)
SELECT count(*)=1 FROM changed;
$function$;

ALTER FUNCTION public.requeue_starlink_adaptive_response_work_v0_1(text,text,text)
  OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.requeue_starlink_adaptive_response_work_v0_1(text,text,text)
  FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.requeue_starlink_adaptive_response_work_v0_1(text,text,text)
  TO leo_analysis;

COMMIT;
