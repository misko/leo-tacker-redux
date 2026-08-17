BEGIN;

-- Bounded exact-ID claims for one reviewed 36-batch deferred-analysis window.
-- Callers cannot use these routines to discover or claim unrelated queue rows.
CREATE FUNCTION public.claim_campaign_analysis_job(
    p_job_ids text[], p_job_type text, p_lease_token text,
    p_ttl_interval interval)
RETURNS SETOF public.job
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE distinct_count integer;
BEGIN
    SELECT pg_catalog.count(DISTINCT value)::integer INTO distinct_count
      FROM pg_catalog.unnest(p_job_ids) AS value;
    IF p_job_ids IS NULL
       OR pg_catalog.cardinality(p_job_ids) NOT BETWEEN 1 AND 72
       OR distinct_count <> pg_catalog.cardinality(p_job_ids)
       OR p_job_type NOT IN (
           'recording_analysis','waterfall_analysis','starlink_suite_analysis'
       )
       OR p_lease_token IS NULL OR p_lease_token = ''
       OR p_ttl_interval IS NULL OR p_ttl_interval <= interval '0' THEN
        RAISE EXCEPTION 'invalid campaign analysis job claim' USING ERRCODE='22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT j.job_id FROM public.job AS j
         WHERE j.job_id = ANY(p_job_ids)
           AND j.job_type=p_job_type
           AND j.available_at_utc <= pg_catalog.clock_timestamp()
           AND (j.state IN ('ready','failed') OR
                (j.state='leased' AND
                 j.lease_expires_utc <= pg_catalog.clock_timestamp()))
         ORDER BY j.available_at_utc,j.job_id
         FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE public.job AS j
       SET state='leased',attempt=j.attempt+1,
           lease_generation=j.lease_generation+1,
           lease_token=p_lease_token,
           lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl_interval,
           last_error=NULL
      FROM candidate WHERE j.job_id=candidate.job_id RETURNING j.*;
END $function$;

CREATE FUNCTION public.claim_campaign_feature_projection(
    p_source_job_ids text[], p_lease_token text, p_ttl_interval interval)
RETURNS TABLE(
    work_id text, work_schema_id text, work_schema_version text,
    source_job_id text, feature_set_id text, analysis_run_id text,
    feature_digest_algorithm text, feature_digest_value text,
    feature_byte_count bigint, feature_media_type text,
    feature_format_id text, feature_locator text,
    recording_id text, recording_digest_algorithm text,
    recording_digest_value text, attempt integer, lease_token text,
    lease_generation bigint, lease_expires_utc timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE distinct_count integer;
BEGIN
    SELECT pg_catalog.count(DISTINCT value)::integer INTO distinct_count
      FROM pg_catalog.unnest(p_source_job_ids) AS value;
    IF p_source_job_ids IS NULL
       OR pg_catalog.cardinality(p_source_job_ids) NOT BETWEEN 1 AND 72
       OR distinct_count <> pg_catalog.cardinality(p_source_job_ids)
       OR p_lease_token IS NULL OR p_lease_token=''
       OR p_ttl_interval IS NULL OR p_ttl_interval <= interval '0' THEN
        RAISE EXCEPTION 'invalid campaign feature projection claim'
            USING ERRCODE='22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT w.work_id FROM public.feature_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids)
           AND w.available_at_utc<=pg_catalog.clock_timestamp()
           AND (w.state IN('ready','failed') OR
                (w.state='leased' AND
                 w.lease_expires_utc<=pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc,w.work_id
         FOR UPDATE SKIP LOCKED LIMIT 1
    ), claimed AS (
        UPDATE public.feature_projection_work AS w
           SET state='leased',attempt=w.attempt+1,
               lease_generation=w.lease_generation+1,
               lease_token=p_lease_token,
               lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl_interval,
               last_error=NULL
          FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*
    )
    SELECT c.work_id,c.work_schema_id,c.work_schema_version,c.source_job_id,
           c.feature_set_id,c.analysis_run_id,c.feature_digest_algorithm,
           c.feature_digest_value,o.byte_count,o.media_type,o.format_id,o.locator,
           c.recording_id,c.recording_digest_algorithm,c.recording_digest_value,
           c.attempt,c.lease_token,c.lease_generation,c.lease_expires_utc
      FROM claimed AS c JOIN public.object_blob AS o
        ON (o.digest_algorithm,o.digest_value)=
           (c.feature_digest_algorithm,c.feature_digest_value)
     WHERE o.lifecycle_state='live';
END $function$;

CREATE FUNCTION public.claim_campaign_waterfall_projection(
    p_source_job_ids text[], p_lease_token text, p_ttl_interval interval)
RETURNS SETOF public.waterfall_projection_work
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE distinct_count integer;
BEGIN
    SELECT pg_catalog.count(DISTINCT value)::integer INTO distinct_count
      FROM pg_catalog.unnest(p_source_job_ids) AS value;
    IF p_source_job_ids IS NULL
       OR pg_catalog.cardinality(p_source_job_ids) NOT BETWEEN 1 AND 72
       OR distinct_count <> pg_catalog.cardinality(p_source_job_ids)
       OR p_lease_token IS NULL OR p_lease_token=''
       OR p_ttl_interval IS NULL OR p_ttl_interval <= interval '0' THEN
        RAISE EXCEPTION 'invalid campaign waterfall projection claim'
            USING ERRCODE='22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT w.work_id FROM public.waterfall_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids)
           AND w.available_at_utc<=pg_catalog.clock_timestamp()
           AND (w.state IN('ready','failed') OR
                (w.state='leased' AND
                 w.lease_expires_utc<=pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc,w.work_id
         FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE public.waterfall_projection_work AS w
       SET state='leased',attempt=w.attempt+1,
           lease_generation=w.lease_generation+1,
           lease_token=p_lease_token,
           lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl_interval,
           last_error=NULL
      FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*;
END $function$;

CREATE FUNCTION public.claim_campaign_starlink_suite_projection(
    p_source_job_ids text[], p_lease_token text, p_ttl_interval interval)
RETURNS SETOF public.starlink_detector_suite_projection_work
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE distinct_count integer;
BEGIN
    SELECT pg_catalog.count(DISTINCT value)::integer INTO distinct_count
      FROM pg_catalog.unnest(p_source_job_ids) AS value;
    IF p_source_job_ids IS NULL
       OR pg_catalog.cardinality(p_source_job_ids) NOT BETWEEN 1 AND 72
       OR distinct_count <> pg_catalog.cardinality(p_source_job_ids)
       OR p_lease_token IS NULL OR p_lease_token=''
       OR p_ttl_interval IS NULL OR p_ttl_interval <= interval '0' THEN
        RAISE EXCEPTION 'invalid campaign Starlink-suite projection claim'
            USING ERRCODE='22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT w.work_id
          FROM public.starlink_detector_suite_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids)
           AND w.available_at_utc<=pg_catalog.clock_timestamp()
           AND (w.state IN('ready','failed') OR
                (w.state='leased' AND
                 w.lease_expires_utc<=pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc,w.work_id
         FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE public.starlink_detector_suite_projection_work AS w
       SET state='leased',attempt=w.attempt+1,
           lease_generation=w.lease_generation+1,
           lease_token=p_lease_token,
           lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl_interval,
           last_error=NULL
      FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*;
END $function$;

CREATE FUNCTION public.read_campaign_analysis_lane_status(
    p_lane text, p_source_job_ids text[])
RETURNS TABLE(identity_id text, state text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE distinct_count integer;
BEGIN
    SELECT pg_catalog.count(DISTINCT value)::integer INTO distinct_count
      FROM pg_catalog.unnest(p_source_job_ids) AS value;
    IF p_source_job_ids IS NULL
       OR pg_catalog.cardinality(p_source_job_ids) NOT BETWEEN 1 AND 72
       OR distinct_count <> pg_catalog.cardinality(p_source_job_ids)
       OR p_lane NOT IN (
           'feature_compute','feature_projection','waterfall_compute',
           'waterfall_projection','starlink_suite_compute',
           'starlink_suite_projection'
       ) THEN
        RAISE EXCEPTION 'invalid campaign analysis lane status request'
            USING ERRCODE='22023';
    END IF;
    IF p_lane IN ('feature_compute','waterfall_compute','starlink_suite_compute') THEN
        RETURN QUERY SELECT j.job_id,j.state FROM public.job AS j
         WHERE j.job_id=ANY(p_source_job_ids);
    ELSIF p_lane='feature_projection' THEN
        RETURN QUERY SELECT w.source_job_id,w.state
          FROM public.feature_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids);
    ELSIF p_lane='waterfall_projection' THEN
        RETURN QUERY SELECT w.source_job_id,w.state
          FROM public.waterfall_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids);
    ELSE
        RETURN QUERY SELECT w.source_job_id,w.state
          FROM public.starlink_detector_suite_projection_work AS w
         WHERE w.source_job_id=ANY(p_source_job_ids);
    END IF;
END $function$;

ALTER FUNCTION public.claim_campaign_analysis_job(text[],text,text,interval)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_campaign_feature_projection(text[],text,interval)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_campaign_waterfall_projection(text[],text,interval)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_campaign_starlink_suite_projection(text[],text,interval)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_campaign_analysis_lane_status(text,text[])
OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION
    public.claim_campaign_analysis_job(text[],text,text,interval),
    public.claim_campaign_feature_projection(text[],text,interval),
    public.claim_campaign_waterfall_projection(text[],text,interval),
    public.claim_campaign_starlink_suite_projection(text[],text,interval),
    public.read_campaign_analysis_lane_status(text,text[])
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;

GRANT EXECUTE ON FUNCTION
    public.claim_campaign_analysis_job(text[],text,text,interval),
    public.claim_campaign_feature_projection(text[],text,interval),
    public.claim_campaign_waterfall_projection(text[],text,interval),
    public.claim_campaign_starlink_suite_projection(text[],text,interval),
    public.read_campaign_analysis_lane_status(text,text[])
TO leo_analysis;

COMMIT;
