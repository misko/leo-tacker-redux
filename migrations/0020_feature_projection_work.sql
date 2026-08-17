BEGIN;

-- Dedicated delivery state for one immutable FeatureSet.  This is intentionally
-- separate from the generic analysis job type: it is an outbox whose creation
-- shares the FeatureSet publication and recording-analysis completion
-- transaction.
CREATE TABLE public.feature_projection_work (
    work_id text PRIMARY KEY
        CHECK (work_id ~ '^fpwork_[0-9a-f]{64}$'),
    work_schema_id text NOT NULL
        CHECK (work_schema_id = 'org.leo-flow.feature-projection-work'),
    work_schema_version text NOT NULL CHECK (work_schema_version = '0.1'),
    source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    feature_set_id text NOT NULL,
    analysis_run_id text NOT NULL,
    feature_digest_algorithm text NOT NULL CHECK (feature_digest_algorithm = 'sha256'),
    feature_digest_value text NOT NULL CHECK (feature_digest_value ~ '^[0-9a-f]{64}$'),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    recording_digest_algorithm text NOT NULL CHECK (recording_digest_algorithm = 'sha256'),
    recording_digest_value text NOT NULL CHECK (recording_digest_value ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready', 'leased', 'failed', 'succeeded', 'parked')),
    available_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_token text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_utc timestamptz,
    last_error text,
    park_reason text,
    parked_at_utc timestamptz,
    projected_at_utc timestamptz,
    created_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (
        feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    ),
    FOREIGN KEY (
        feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    ) REFERENCES public.feature_set (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value
    ),
    CHECK (
        (state = 'leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL)
        OR
        (state <> 'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)
    ),
    CHECK (
        (state = 'parked'
         AND park_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
         AND parked_at_utc IS NOT NULL
         AND projected_at_utc IS NULL)
        OR
        (state <> 'parked' AND park_reason IS NULL AND parked_at_utc IS NULL)
    ),
    CHECK (
        (state = 'succeeded' AND projected_at_utc IS NOT NULL)
        OR
        (state <> 'succeeded' AND projected_at_utc IS NULL)
    )
);

CREATE INDEX feature_projection_work_claim_idx
    ON public.feature_projection_work(available_at_utc, work_id)
    WHERE state IN ('ready', 'failed', 'leased');

CREATE FUNCTION public.publish_feature_projection_work(
    p_work_id text,
    p_source_job_id text,
    p_source_lease_token text,
    p_source_lease_generation bigint,
    p_feature_set_id text,
    p_analysis_run_id text,
    p_feature_digest_algorithm text,
    p_feature_digest_value text,
    p_recording_id text,
    p_recording_digest_algorithm text,
    p_recording_digest_value text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    inserted_work_id text;
    conflict_count bigint;
    conflict_exact boolean;
BEGIN
    IF p_work_id !~ '^fpwork_[0-9a-f]{64}$'
       OR p_source_job_id !~ '^job_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_source_lease_token IS NULL OR p_source_lease_token = ''
       OR p_source_lease_generation <= 0
       OR p_feature_set_id !~ '^fset_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_analysis_run_id !~ '^arun_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_feature_digest_algorithm <> 'sha256'
       OR p_feature_digest_value !~ '^[0-9a-f]{64}$'
       OR p_recording_id !~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_recording_digest_algorithm <> 'sha256'
       OR p_recording_digest_value !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid feature projection work publication'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
      FROM public.job AS j
     WHERE j.job_id = p_source_job_id
       AND j.job_type = 'recording_analysis'
       AND j.state = 'leased'
       AND j.lease_token = p_source_lease_token
       AND j.lease_generation = p_source_lease_generation
       AND j.lease_expires_utc > pg_catalog.clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'recording-analysis lease is not current'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1
      FROM public.feature_set AS f
      JOIN public.object_blob AS o
        ON (o.digest_algorithm, o.digest_value) =
           (f.bundle_digest_algorithm, f.bundle_digest_value)
     WHERE f.feature_set_id = p_feature_set_id
       AND f.analysis_run_id = p_analysis_run_id
       AND f.bundle_digest_algorithm = p_feature_digest_algorithm
       AND f.bundle_digest_value = p_feature_digest_value
       AND f.recording_id = p_recording_id
       AND f.input_recording_digest_algorithm = p_recording_digest_algorithm
       AND f.input_recording_digest_value = p_recording_digest_value
       AND o.lifecycle_state = 'live';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'feature projection source is not exact and authoritative'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO public.feature_projection_work(
        work_id, work_schema_id, work_schema_version, source_job_id,
        feature_set_id, analysis_run_id, feature_digest_algorithm,
        feature_digest_value, recording_id, recording_digest_algorithm,
        recording_digest_value)
    VALUES (
        p_work_id, 'org.leo-flow.feature-projection-work', '0.1', p_source_job_id,
        p_feature_set_id, p_analysis_run_id, p_feature_digest_algorithm,
        p_feature_digest_value, p_recording_id, p_recording_digest_algorithm,
        p_recording_digest_value)
    ON CONFLICT DO NOTHING
    RETURNING work_id INTO inserted_work_id;

    IF inserted_work_id IS NULL THEN
        SELECT pg_catalog.count(*), pg_catalog.bool_and(
                   work_id = p_work_id
               AND source_job_id = p_source_job_id
               AND feature_set_id = p_feature_set_id
               AND analysis_run_id = p_analysis_run_id
               AND feature_digest_algorithm = p_feature_digest_algorithm
               AND feature_digest_value = p_feature_digest_value
               AND recording_id = p_recording_id
               AND recording_digest_algorithm = p_recording_digest_algorithm
               AND recording_digest_value = p_recording_digest_value)
          INTO conflict_count, conflict_exact
          FROM public.feature_projection_work
         WHERE work_id = p_work_id
            OR source_job_id = p_source_job_id
            OR (feature_set_id = p_feature_set_id
                AND analysis_run_id = p_analysis_run_id
                AND feature_digest_algorithm = p_feature_digest_algorithm
                AND feature_digest_value = p_feature_digest_value);
        IF conflict_count <> 1 OR conflict_exact IS NOT TRUE THEN
            RAISE EXCEPTION 'feature projection work identity identifies different content'
                USING ERRCODE = '23505';
        END IF;
    END IF;
    RETURN true;
END
$function$;

CREATE FUNCTION public.claim_feature_projection_work(
    p_lease_token text, p_ttl_interval interval)
RETURNS TABLE(
    work_id text, work_schema_id text, work_schema_version text,
    source_job_id text, feature_set_id text, analysis_run_id text,
    feature_digest_algorithm text, feature_digest_value text,
    feature_byte_count bigint, feature_media_type text,
    feature_format_id text, feature_locator text,
    recording_id text, recording_digest_algorithm text,
    recording_digest_value text, attempt integer, lease_token text,
    lease_generation bigint, lease_expires_utc timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lease_token IS NULL OR p_lease_token = ''
       OR p_ttl_interval <= interval '0 seconds' THEN
        RAISE EXCEPTION 'invalid feature projection claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT w.work_id
          FROM public.feature_projection_work AS w
         WHERE w.available_at_utc <= pg_catalog.clock_timestamp()
           AND (w.state IN ('ready', 'failed')
                OR (w.state = 'leased'
                    AND w.lease_expires_utc <= pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc, w.work_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    ), claimed AS (
        UPDATE public.feature_projection_work AS w
           SET state = 'leased', attempt = w.attempt + 1,
               lease_generation = w.lease_generation + 1,
               lease_token = p_lease_token,
               lease_expires_utc = pg_catalog.clock_timestamp() + p_ttl_interval,
               last_error = NULL
          FROM candidate
         WHERE w.work_id = candidate.work_id
         RETURNING w.*
    )
    SELECT c.work_id, c.work_schema_id, c.work_schema_version,
           c.source_job_id, c.feature_set_id, c.analysis_run_id,
           c.feature_digest_algorithm, c.feature_digest_value,
           o.byte_count, o.media_type, o.format_id, o.locator,
           c.recording_id, c.recording_digest_algorithm,
           c.recording_digest_value, c.attempt, c.lease_token,
           c.lease_generation, c.lease_expires_utc
      FROM claimed AS c
      JOIN public.object_blob AS o
        ON (o.digest_algorithm, o.digest_value) =
           (c.feature_digest_algorithm, c.feature_digest_value)
     WHERE o.lifecycle_state = 'live';
END
$function$;

CREATE FUNCTION public.heartbeat_feature_projection_work(
    p_work_id text, p_lease_token text, p_lease_generation bigint,
    p_ttl_interval interval)
RETURNS TABLE(
    work_id text, work_schema_id text, work_schema_version text,
    source_job_id text, feature_set_id text, analysis_run_id text,
    feature_digest_algorithm text, feature_digest_value text,
    feature_byte_count bigint, feature_media_type text,
    feature_format_id text, feature_locator text,
    recording_id text, recording_digest_algorithm text,
    recording_digest_value text, attempt integer, lease_token text,
    lease_generation bigint, lease_expires_utc timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH renewed AS (
        UPDATE public.feature_projection_work AS w
           SET lease_expires_utc = pg_catalog.clock_timestamp() + p_ttl_interval
         WHERE w.work_id = p_work_id AND w.state = 'leased'
           AND w.lease_token = p_lease_token
           AND w.lease_generation = p_lease_generation
           AND w.lease_expires_utc > pg_catalog.clock_timestamp()
           AND p_ttl_interval > interval '0 seconds'
         RETURNING w.*
    )
    SELECT r.work_id, r.work_schema_id, r.work_schema_version,
           r.source_job_id, r.feature_set_id, r.analysis_run_id,
           r.feature_digest_algorithm, r.feature_digest_value,
           o.byte_count, o.media_type, o.format_id, o.locator,
           r.recording_id, r.recording_digest_algorithm,
           r.recording_digest_value, r.attempt, r.lease_token,
           r.lease_generation, r.lease_expires_utc
      FROM renewed AS r
      JOIN public.object_blob AS o
        ON (o.digest_algorithm, o.digest_value) =
           (r.feature_digest_algorithm, r.feature_digest_value)
     WHERE o.lifecycle_state = 'live';
$function$;

CREATE FUNCTION public.complete_feature_projection_work(
    p_work_id text, p_lease_token text, p_lease_generation bigint)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH completed AS (
        UPDATE public.feature_projection_work AS w
           SET state = 'succeeded', projected_at_utc = pg_catalog.clock_timestamp(),
               lease_token = NULL, lease_expires_utc = NULL, last_error = NULL
         WHERE w.work_id = p_work_id AND w.state = 'leased'
           AND w.lease_token = p_lease_token
           AND w.lease_generation = p_lease_generation
           AND w.lease_expires_utc > pg_catalog.clock_timestamp()
         RETURNING w.work_id)
    SELECT pg_catalog.count(*) = 1 FROM completed;
$function$;

CREATE FUNCTION public.retry_feature_projection_work(
    p_work_id text, p_lease_token text, p_lease_generation bigint,
    p_reason text, p_delay_interval interval)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH failed AS (
        UPDATE public.feature_projection_work AS w
           SET state = 'failed', last_error = p_reason,
               available_at_utc = pg_catalog.clock_timestamp() + p_delay_interval,
               lease_token = NULL, lease_expires_utc = NULL
         WHERE w.work_id = p_work_id AND w.state = 'leased'
           AND w.lease_token = p_lease_token
           AND w.lease_generation = p_lease_generation
           AND w.lease_expires_utc > pg_catalog.clock_timestamp()
           AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
           AND p_delay_interval > interval '0 seconds'
         RETURNING w.work_id)
    SELECT pg_catalog.count(*) = 1 FROM failed;
$function$;

CREATE FUNCTION public.park_feature_projection_work(
    p_work_id text, p_lease_token text, p_lease_generation bigint,
    p_reason text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH parked AS (
        UPDATE public.feature_projection_work AS w
           SET state = 'parked', park_reason = p_reason,
               parked_at_utc = pg_catalog.clock_timestamp(),
               lease_token = NULL, lease_expires_utc = NULL,
               last_error = NULL
         WHERE w.work_id = p_work_id AND w.state = 'leased'
           AND w.lease_token = p_lease_token
           AND w.lease_generation = p_lease_generation
           AND w.lease_expires_utc > pg_catalog.clock_timestamp()
           AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
         RETURNING w.work_id)
    SELECT pg_catalog.count(*) = 1 FROM parked;
$function$;

GRANT SELECT, INSERT, UPDATE ON public.feature_projection_work
TO leo_routine_owner;

ALTER FUNCTION public.publish_feature_projection_work(
    text, text, text, bigint, text, text, text, text, text, text, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_feature_projection_work(text, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.heartbeat_feature_projection_work(text, text, bigint, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_feature_projection_work(text, text, bigint)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_feature_projection_work(text, text, bigint, text, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_feature_projection_work(text, text, bigint, text)
    OWNER TO leo_routine_owner;

REVOKE ALL ON public.feature_projection_work
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION
    public.publish_feature_projection_work(
        text, text, text, bigint, text, text, text, text, text, text, text),
    public.claim_feature_projection_work(text, interval),
    public.heartbeat_feature_projection_work(text, text, bigint, interval),
    public.complete_feature_projection_work(text, text, bigint),
    public.retry_feature_projection_work(text, text, bigint, text, interval),
    public.park_feature_projection_work(text, text, bigint, text)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION
    public.publish_feature_projection_work(
        text, text, text, bigint, text, text, text, text, text, text, text),
    public.claim_feature_projection_work(text, interval),
    public.heartbeat_feature_projection_work(text, text, bigint, interval),
    public.complete_feature_projection_work(text, text, bigint),
    public.retry_feature_projection_work(text, text, bigint, text, interval),
    public.park_feature_projection_work(text, text, bigint, text)
TO leo_analysis;

COMMIT;
