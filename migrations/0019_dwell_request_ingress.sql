BEGIN;

-- Immutable authenticated ingress metadata. The existing job table remains the
-- only workflow state machine; this table only binds one versioned request to
-- its exact source catalogs, digest, route, expiry, and idempotency identity.
CREATE TABLE public.dwell_request_ingress (
    request_id text PRIMARY KEY
        CHECK (request_id ~ '^dwell_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    request_schema_id text NOT NULL
        CHECK (request_schema_id = 'org.leo-flow.dwell-request'),
    request_schema_version text NOT NULL CHECK (request_schema_version = '0.1'),
    request_digest_algorithm text NOT NULL
        CHECK (request_digest_algorithm = 'sha256'),
    request_digest_value text NOT NULL UNIQUE
        CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[^[:space:]]+$'),
    source_recording_id text NOT NULL REFERENCES public.recording(recording_id),
    source_recording_digest_algorithm text NOT NULL
        CHECK (source_recording_digest_algorithm = 'sha256'),
    source_recording_digest_value text NOT NULL
        CHECK (source_recording_digest_value ~ '^[0-9a-f]{64}$'),
    source_feature_set_id text NOT NULL,
    source_analysis_run_id text NOT NULL,
    source_feature_digest_algorithm text NOT NULL
        CHECK (source_feature_digest_algorithm = 'sha256'),
    source_feature_digest_value text NOT NULL,
    station_id text NOT NULL
        CHECK (station_id ~ '^station_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    radio_id text NOT NULL
        CHECK (radio_id ~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    issued_utc_ns bigint NOT NULL CHECK (issued_utc_ns >= 0),
    expires_utc_ns bigint NOT NULL CHECK (expires_utc_ns > issued_utc_ns),
    published_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    FOREIGN KEY (
        source_feature_set_id, source_analysis_run_id,
        source_feature_digest_algorithm, source_feature_digest_value
    ) REFERENCES public.feature_set (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value
    ),
    CHECK (expires_utc_ns <= issued_utc_ns + 300000000000)
);

CREATE INDEX dwell_request_route_claim_idx
    ON public.dwell_request_ingress(station_id, radio_id, expires_utc_ns, job_id);

CREATE FUNCTION public.publish_dwell_request(p_publication jsonb)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    inserted_job_id text;
    inserted_request_id text;
    conflict_count bigint;
    conflict_exact boolean;
    p_payload jsonb := p_publication -> 'payload';
BEGIN
    IF p_publication IS NULL
       OR pg_catalog.jsonb_typeof(p_publication) <> 'object'
       OR pg_catalog.jsonb_typeof(p_payload) <> 'object'
       OR pg_catalog.octet_length(p_publication::text) > 65536
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_publication)) <> 19
       OR p_publication ->> 'request_schema_id' <> 'org.leo-flow.dwell-request'
       OR p_publication ->> 'request_schema_version' <> '0.1'
       OR p_publication ->> 'request_digest_algorithm' <> 'sha256'
       OR p_publication ->> 'request_digest_value' !~ '^[0-9a-f]{64}$'
       OR p_publication ->> 'job_id' !~ '^job_dwell_[0-9a-f]{64}$'
       OR p_publication ->> 'job_id' <>
          'job_dwell_' || (p_publication ->> 'request_digest_value')
       OR p_publication ->> 'request_id' !~ '^dwell_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_publication ->> 'idempotency_key' !~ '^[^[:space:]]+$'
       OR p_publication ->> 'station_id' !~ '^station_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_publication ->> 'radio_id' !~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_publication ->> 'source_recording_digest_algorithm' <> 'sha256'
       OR p_publication ->> 'source_feature_digest_algorithm' <> 'sha256'
       OR p_publication ->> 'source_recording_digest_value' !~ '^[0-9a-f]{64}$'
       OR p_publication ->> 'source_feature_digest_value' !~ '^[0-9a-f]{64}$'
       OR (p_publication ->> 'issued_utc_ns')::bigint < 0
       OR (p_publication ->> 'expires_utc_ns')::bigint <=
          (p_publication ->> 'issued_utc_ns')::bigint
       OR (p_publication ->> 'expires_utc_ns')::bigint >
          (p_publication ->> 'issued_utc_ns')::bigint + 300000000000
       OR p_payload #>> '{schema,schema_id}' <>
          p_publication ->> 'request_schema_id'
       OR (p_payload #>> '{schema,version,major}')::integer <> 0
       OR (p_payload #>> '{schema,version,minor}')::integer <> 1
       OR p_payload ->> 'request_id' <> p_publication ->> 'request_id'
       OR p_payload ->> 'idempotency_key' <> p_publication ->> 'idempotency_key'
       OR p_payload ->> 'station_id' <> p_publication ->> 'station_id'
       OR p_payload ->> 'radio_id' <> p_publication ->> 'radio_id'
       OR (p_payload ->> 'issued_utc_ns')::bigint <>
          (p_publication ->> 'issued_utc_ns')::bigint
       OR (p_payload ->> 'expires_utc_ns')::bigint <>
          (p_publication ->> 'expires_utc_ns')::bigint
       OR p_payload #>> '{source,recording_id}' <>
          p_publication ->> 'source_recording_id'
       OR p_payload #>> '{source,recording_identity_digest,algorithm}' <>
          p_publication ->> 'source_recording_digest_algorithm'
       OR p_payload #>> '{source,recording_identity_digest,value}' <>
          p_publication ->> 'source_recording_digest_value'
       OR p_payload #>> '{source,feature_set_ref,feature_set_id}' <>
          p_publication ->> 'source_feature_set_id'
       OR p_payload #>> '{source,feature_set_ref,analysis_run_id}' <>
          p_publication ->> 'source_analysis_run_id'
       OR p_payload #>> '{source,feature_set_ref,bundle_ref,digest,algorithm}' <>
          p_publication ->> 'source_feature_digest_algorithm'
       OR p_payload #>> '{source,feature_set_ref,bundle_ref,digest,value}' <>
          p_publication ->> 'source_feature_digest_value' THEN
        RAISE EXCEPTION 'invalid dwell request publication'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
      FROM public.feature_set AS f
      JOIN public.object_blob AS o
        ON (o.digest_algorithm, o.digest_value) =
           (f.bundle_digest_algorithm, f.bundle_digest_value)
     WHERE f.feature_set_id = p_publication ->> 'source_feature_set_id'
       AND f.analysis_run_id = p_publication ->> 'source_analysis_run_id'
       AND f.recording_id = p_publication ->> 'source_recording_id'
       AND f.input_recording_digest_algorithm =
           p_publication ->> 'source_recording_digest_algorithm'
       AND f.input_recording_digest_value =
           p_publication ->> 'source_recording_digest_value'
       AND f.bundle_digest_algorithm =
           p_publication ->> 'source_feature_digest_algorithm'
       AND f.bundle_digest_value = p_publication ->> 'source_feature_digest_value'
       AND o.byte_count =
           (p_payload #>> '{source,feature_set_ref,bundle_ref,byte_count}')::bigint
       AND o.media_type =
           p_payload #>> '{source,feature_set_ref,bundle_ref,media_type}'
       AND o.format_id =
           p_payload #>> '{source,feature_set_ref,bundle_ref,format_id}'
       AND o.locator = p_payload #>> '{source,feature_set_ref,bundle_ref,locator}'
       AND o.lifecycle_state = 'live';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'dwell request source is not exact and authoritative'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO public.job(
        job_id, job_type, payload_schema_id, payload_schema_version,
        payload, state, available_at_utc)
    VALUES (
        p_publication ->> 'job_id', 'dwell_capture',
        p_publication ->> 'request_schema_id',
        p_publication ->> 'request_schema_version', p_payload, 'ready',
        pg_catalog.to_timestamp(
            ((p_publication ->> 'issued_utc_ns')::numeric / 1000000000)))
    ON CONFLICT (job_id) DO NOTHING
    RETURNING job_id INTO inserted_job_id;

    IF inserted_job_id IS NULL THEN
        PERFORM 1 FROM public.job
         WHERE job_id = p_publication ->> 'job_id'
           AND job_type = 'dwell_capture'
           AND payload_schema_id = p_publication ->> 'request_schema_id'
           AND payload_schema_version = p_publication ->> 'request_schema_version'
           AND payload = p_payload;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'dwell job identity identifies different content'
                USING ERRCODE = '23505';
        END IF;
    END IF;

    INSERT INTO public.dwell_request_ingress(
        request_id, job_id, request_schema_id, request_schema_version,
        request_digest_algorithm, request_digest_value, idempotency_key,
        source_recording_id, source_recording_digest_algorithm,
        source_recording_digest_value, source_feature_set_id,
        source_analysis_run_id, source_feature_digest_algorithm,
        source_feature_digest_value, station_id, radio_id,
        issued_utc_ns, expires_utc_ns)
    VALUES (
        p_publication ->> 'request_id', p_publication ->> 'job_id',
        p_publication ->> 'request_schema_id',
        p_publication ->> 'request_schema_version',
        p_publication ->> 'request_digest_algorithm',
        p_publication ->> 'request_digest_value',
        p_publication ->> 'idempotency_key',
        p_publication ->> 'source_recording_id',
        p_publication ->> 'source_recording_digest_algorithm',
        p_publication ->> 'source_recording_digest_value',
        p_publication ->> 'source_feature_set_id',
        p_publication ->> 'source_analysis_run_id',
        p_publication ->> 'source_feature_digest_algorithm',
        p_publication ->> 'source_feature_digest_value',
        p_publication ->> 'station_id', p_publication ->> 'radio_id',
        (p_publication ->> 'issued_utc_ns')::bigint,
        (p_publication ->> 'expires_utc_ns')::bigint)
    ON CONFLICT DO NOTHING
    RETURNING request_id INTO inserted_request_id;

    IF inserted_request_id IS NULL THEN
        SELECT pg_catalog.count(*), pg_catalog.bool_and(
                   request_id = p_publication ->> 'request_id'
               AND job_id = p_publication ->> 'job_id'
               AND request_digest_algorithm =
                   p_publication ->> 'request_digest_algorithm'
               AND request_digest_value = p_publication ->> 'request_digest_value'
               AND idempotency_key = p_publication ->> 'idempotency_key'
               AND source_recording_id = p_publication ->> 'source_recording_id'
               AND source_recording_digest_value =
                   p_publication ->> 'source_recording_digest_value'
               AND source_feature_set_id =
                   p_publication ->> 'source_feature_set_id'
               AND source_analysis_run_id =
                   p_publication ->> 'source_analysis_run_id'
               AND source_feature_digest_value =
                   p_publication ->> 'source_feature_digest_value'
               AND station_id = p_publication ->> 'station_id'
               AND radio_id = p_publication ->> 'radio_id'
               AND issued_utc_ns =
                   (p_publication ->> 'issued_utc_ns')::bigint
               AND expires_utc_ns =
                   (p_publication ->> 'expires_utc_ns')::bigint)
          INTO conflict_count, conflict_exact
          FROM public.dwell_request_ingress
         WHERE request_id = p_publication ->> 'request_id'
            OR job_id = p_publication ->> 'job_id'
            OR request_digest_value = p_publication ->> 'request_digest_value'
            OR idempotency_key = p_publication ->> 'idempotency_key';
        IF conflict_count <> 1 OR conflict_exact IS NOT TRUE THEN
            RAISE EXCEPTION 'dwell request identity identifies different content'
                USING ERRCODE = '23505';
        END IF;
    END IF;
    RETURN inserted_request_id IS NOT NULL;
END
$function$;

CREATE FUNCTION public.claim_dwell_request(
    p_station_id text, p_radio_id text, p_lease_token text, p_ttl_interval interval)
RETURNS TABLE(
    job_id text, payload_schema_id text, payload_schema_version text,
    payload jsonb, attempt integer, lease_token text,
    lease_generation bigint, lease_expires_utc timestamptz,
    request_digest_algorithm text, request_digest_value text,
    request_id text, idempotency_key text, issued_utc_ns bigint,
    expires_utc_ns bigint, station_id text, radio_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_station_id !~ '^station_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_radio_id !~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_lease_token IS NULL OR p_lease_token = ''
       OR p_ttl_interval <= interval '0 seconds' THEN
        RAISE EXCEPTION 'invalid dwell request claim' USING ERRCODE = '22023';
    END IF;

    UPDATE public.job AS j
       SET state = 'parked', park_reason = 'request_expired',
           parked_at_utc = pg_catalog.clock_timestamp(),
           lease_token = NULL, lease_expires_utc = NULL,
           result_ref = NULL, last_error = NULL
      FROM public.dwell_request_ingress AS d
     WHERE j.job_id = d.job_id
       AND d.station_id = p_station_id AND d.radio_id = p_radio_id
       AND d.expires_utc_ns <=
           (pg_catalog.date_part('epoch', pg_catalog.clock_timestamp()) *
            1000000000)::bigint
       AND (j.state IN ('ready', 'failed') OR
            (j.state = 'leased' AND
             j.lease_expires_utc <= pg_catalog.clock_timestamp()));

    RETURN QUERY
    WITH candidate AS (
        SELECT j.job_id
          FROM public.job AS j
          JOIN public.dwell_request_ingress AS d ON d.job_id = j.job_id
         WHERE j.job_type = 'dwell_capture'
           AND d.station_id = p_station_id AND d.radio_id = p_radio_id
           AND d.issued_utc_ns <=
               (pg_catalog.date_part('epoch', pg_catalog.clock_timestamp()) *
                1000000000)::bigint
           AND d.expires_utc_ns >
               (pg_catalog.date_part('epoch', pg_catalog.clock_timestamp()) *
                1000000000)::bigint
           AND j.available_at_utc <= pg_catalog.clock_timestamp()
           AND (j.state IN ('ready', 'failed') OR
                (j.state = 'leased' AND
                 j.lease_expires_utc <= pg_catalog.clock_timestamp()))
         ORDER BY j.available_at_utc, j.job_id
         FOR UPDATE OF j SKIP LOCKED
         LIMIT 1
    ), claimed AS (
        UPDATE public.job AS j
           SET state = 'leased', attempt = j.attempt + 1,
               lease_generation = j.lease_generation + 1,
               lease_token = p_lease_token,
               lease_expires_utc = least(
                   pg_catalog.clock_timestamp() + p_ttl_interval,
                   pg_catalog.to_timestamp(d.expires_utc_ns::numeric / 1000000000))
          FROM candidate, public.dwell_request_ingress AS d
         WHERE j.job_id = candidate.job_id AND d.job_id = j.job_id
         RETURNING j.*
    )
    SELECT c.job_id, c.payload_schema_id, c.payload_schema_version,
           c.payload, c.attempt, c.lease_token, c.lease_generation,
           c.lease_expires_utc, d.request_digest_algorithm,
           d.request_digest_value, d.request_id, d.idempotency_key,
           d.issued_utc_ns, d.expires_utc_ns, d.station_id, d.radio_id
      FROM claimed AS c
      JOIN public.dwell_request_ingress AS d ON d.job_id = c.job_id;
END
$function$;

CREATE FUNCTION public.heartbeat_dwell_request(
    p_job_id text, p_lease_token text, p_lease_generation bigint,
    p_ttl_interval interval)
RETURNS timestamptz
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.job AS j
       SET lease_expires_utc = least(
           pg_catalog.clock_timestamp() + p_ttl_interval,
           pg_catalog.to_timestamp(d.expires_utc_ns::numeric / 1000000000))
      FROM public.dwell_request_ingress AS d
     WHERE j.job_id = p_job_id AND d.job_id = j.job_id
       AND j.job_type = 'dwell_capture' AND j.state = 'leased'
       AND j.lease_token = p_lease_token
       AND j.lease_generation = p_lease_generation
       AND j.lease_expires_utc > pg_catalog.clock_timestamp()
       AND d.expires_utc_ns >
           (pg_catalog.date_part('epoch', pg_catalog.clock_timestamp()) *
            1000000000)::bigint
       AND p_ttl_interval > interval '0 seconds'
     RETURNING j.lease_expires_utc;
$function$;

CREATE FUNCTION public.complete_dwell_request(
    p_job_id text, p_lease_token text, p_lease_generation bigint,
    p_result_ref jsonb)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH completed AS (
        UPDATE public.job AS j
           SET state = 'succeeded', result_ref = p_result_ref,
               lease_token = NULL, lease_expires_utc = NULL
          FROM public.dwell_request_ingress AS d
         WHERE j.job_id = p_job_id AND d.job_id = j.job_id
           AND j.job_type = 'dwell_capture' AND j.state = 'leased'
           AND j.lease_token = p_lease_token
           AND j.lease_generation = p_lease_generation
           AND j.lease_expires_utc > pg_catalog.clock_timestamp()
           AND d.expires_utc_ns >
               (pg_catalog.date_part('epoch', pg_catalog.clock_timestamp()) *
                1000000000)::bigint
           AND pg_catalog.jsonb_typeof(p_result_ref) = 'object'
           AND (SELECT pg_catalog.count(*)
                  FROM pg_catalog.jsonb_object_keys(p_result_ref)) = 5
           AND p_result_ref ->> 'artifact_id' = 'plan_' || d.request_id
           AND p_result_ref ->> 'digest_algorithm' = 'sha256'
           AND p_result_ref ->> 'digest_value' ~ '^[0-9a-f]{64}$'
           AND p_result_ref ->> 'schema_id' = 'org.leo-flow.capture-plan'
           AND p_result_ref ->> 'schema_version' = '0.1'
         RETURNING j.job_id)
    SELECT pg_catalog.count(*) = 1 FROM completed;
$function$;

CREATE FUNCTION public.fail_dwell_request(
    p_job_id text, p_lease_token text, p_lease_generation bigint,
    p_reason text, p_retry_at_utc timestamptz)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH failed AS (
        UPDATE public.job AS j
           SET state = 'failed', last_error = p_reason,
               available_at_utc = p_retry_at_utc,
               lease_token = NULL, lease_expires_utc = NULL
          FROM public.dwell_request_ingress AS d
         WHERE j.job_id = p_job_id AND d.job_id = j.job_id
           AND j.job_type = 'dwell_capture' AND j.state = 'leased'
           AND j.lease_token = p_lease_token
           AND j.lease_generation = p_lease_generation
           AND j.lease_expires_utc > pg_catalog.clock_timestamp()
           AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
           AND p_retry_at_utc > pg_catalog.clock_timestamp()
           AND p_retry_at_utc <
               pg_catalog.to_timestamp(d.expires_utc_ns::numeric / 1000000000)
         RETURNING j.job_id)
    SELECT pg_catalog.count(*) = 1 FROM failed;
$function$;

CREATE FUNCTION public.park_dwell_request(
    p_job_id text, p_lease_token text, p_lease_generation bigint, p_reason text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH parked AS (
        UPDATE public.job AS j
           SET state = 'parked', park_reason = p_reason,
               parked_at_utc = pg_catalog.clock_timestamp(),
               lease_token = NULL, lease_expires_utc = NULL,
               result_ref = NULL, last_error = NULL
          FROM public.dwell_request_ingress AS d
         WHERE j.job_id = p_job_id AND d.job_id = j.job_id
           AND j.job_type = 'dwell_capture' AND j.state = 'leased'
           AND j.lease_token = p_lease_token
           AND j.lease_generation = p_lease_generation
           AND j.lease_expires_utc > pg_catalog.clock_timestamp()
           AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
         RETURNING j.job_id)
    SELECT pg_catalog.count(*) = 1 FROM parked;
$function$;

GRANT SELECT, INSERT ON public.dwell_request_ingress TO leo_routine_owner;
GRANT SELECT ON public.feature_set TO leo_routine_owner;

ALTER FUNCTION public.publish_dwell_request(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_dwell_request(text, text, text, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.heartbeat_dwell_request(text, text, bigint, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_dwell_request(text, text, bigint, jsonb)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.fail_dwell_request(text, text, bigint, text, timestamptz)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_dwell_request(text, text, bigint, text)
    OWNER TO leo_routine_owner;

REVOKE ALL ON public.dwell_request_ingress
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.publish_dwell_request(jsonb),
    public.claim_dwell_request(text, text, text, interval),
    public.heartbeat_dwell_request(text, text, bigint, interval),
    public.complete_dwell_request(text, text, bigint, jsonb),
    public.fail_dwell_request(text, text, bigint, text, timestamptz),
    public.park_dwell_request(text, text, bigint, text)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION public.publish_dwell_request(jsonb) TO leo_analysis;
GRANT EXECUTE ON FUNCTION
    public.claim_dwell_request(text, text, text, interval),
    public.heartbeat_dwell_request(text, text, bigint, interval),
    public.complete_dwell_request(text, text, bigint, jsonb),
    public.fail_dwell_request(text, text, bigint, text, timestamptz),
    public.park_dwell_request(text, text, bigint, text)
TO leo_capture;

COMMIT;
