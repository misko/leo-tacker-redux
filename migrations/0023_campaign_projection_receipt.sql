BEGIN;

-- A finite campaign must prove the exact durable projection corresponding to
-- each recording-analysis job.  The dashboard's recording-level COMPLETE flag
-- is a useful presentation postcondition, but it deliberately does not expose
-- job or FeatureSet identities.  Keep the outbox table private and expose one
-- exact, read-only receipt through the analysis capability role.
CREATE FUNCTION public.read_feature_projection_receipt(p_source_job_id text)
RETURNS TABLE(
    work_id text,
    source_job_id text,
    work_state text,
    feature_set_id text,
    analysis_run_id text,
    feature_digest_algorithm text,
    feature_digest_value text,
    feature_byte_count bigint,
    feature_media_type text,
    feature_format_id text,
    feature_locator text,
    recording_id text,
    recording_digest_algorithm text,
    recording_digest_value text,
    projected_at_utc timestamptz,
    job_state text,
    job_result_ref jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT w.work_id,
           w.source_job_id,
           w.state,
           w.feature_set_id,
           w.analysis_run_id,
           w.feature_digest_algorithm,
           w.feature_digest_value,
           o.byte_count,
           o.media_type,
           o.format_id,
           o.locator,
           w.recording_id,
           w.recording_digest_algorithm,
           w.recording_digest_value,
           w.projected_at_utc,
           j.state,
           j.result_ref
      FROM public.feature_projection_work AS w
      JOIN public.job AS j ON j.job_id = w.source_job_id
      JOIN public.object_blob AS o
        ON (o.digest_algorithm, o.digest_value) =
           (w.feature_digest_algorithm, w.feature_digest_value)
     WHERE p_source_job_id ~ '^job_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       AND w.source_job_id = p_source_job_id
       AND j.job_type = 'recording_analysis'
       AND o.lifecycle_state = 'live';
$function$;

ALTER FUNCTION public.read_feature_projection_receipt(text)
    OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.read_feature_projection_receipt(text)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION public.read_feature_projection_receipt(text)
TO leo_analysis;

COMMIT;
