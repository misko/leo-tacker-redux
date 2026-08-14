BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'leo_routine_owner') THEN
        CREATE ROLE leo_routine_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$roles$;

REVOKE CREATE ON SCHEMA public
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance,
     leo_routine_owner;
GRANT USAGE ON SCHEMA public TO leo_routine_owner;

GRANT SELECT, INSERT, UPDATE ON public.object_blob TO leo_routine_owner;
GRANT SELECT ON public.object_retention_status TO leo_routine_owner;
GRANT INSERT ON public.object_gc_attempt TO leo_routine_owner;
GRANT USAGE, SELECT ON SEQUENCE public.object_gc_attempt_attempt_id_seq
TO leo_routine_owner;
GRANT SELECT, INSERT, UPDATE ON public.object_orphan_observation
TO leo_routine_owner;
GRANT SELECT, INSERT ON public.object_orphan_event TO leo_routine_owner;
GRANT USAGE, SELECT ON SEQUENCE public.object_orphan_event_event_id_seq
TO leo_routine_owner;
GRANT SELECT, INSERT, UPDATE ON public.job TO leo_routine_owner;
GRANT SELECT, INSERT ON public.tracking_input_snapshot, public.tracking_input_entry
    TO leo_routine_owner;
GRANT SELECT ON public.recording_hardware_link, public.hardware_receiver_chain
    TO leo_routine_owner;

CREATE OR REPLACE FUNCTION public.object_blob_assert_live_reference()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    referenced_algorithm text := pg_catalog.to_jsonb(NEW) ->> TG_ARGV[0];
    referenced_digest text := pg_catalog.to_jsonb(NEW) ->> TG_ARGV[1];
BEGIN
    PERFORM 1
      FROM public.object_blob
     WHERE digest_algorithm = referenced_algorithm
       AND digest_value = referenced_digest
       AND lifecycle_state = 'live'
     FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'object reference does not identify a live catalog object'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END
$function$;

ALTER FUNCTION public.object_blob_assert_live_reference()
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.object_digest_fence(text, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.register_live_object_blob(text, text, bigint, text, text, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.gc_claim_object(text, text, text, timestamptz, timestamptz)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.gc_record_delete_failure(text, text, text, timestamptz, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.gc_complete_object_delete(text, text, text, timestamptz)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.observe_unregistered_object(
    text, text, bigint, text, bigint, bigint, bigint, bigint, bigint
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_unregistered_object(
    text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text, bigint
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.orphan_claim_is_current(
    text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_unregistered_object_delete(text, text, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.record_unregistered_object_delete_failure(text, text, text, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.enqueue_job(text, text, text, text, jsonb, timestamptz)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_job(text[], text, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.heartbeat_job(text, text, bigint, interval)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.lock_active_job_lease(text, text, text, bigint)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_job(text, text, bigint, jsonb)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.fail_job(text, text, bigint, text, timestamptz)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_job(text, text, bigint, text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_tracking_input_snapshot(jsonb)
    OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.object_blob_assert_live_reference(),
    public.object_digest_fence(text, text),
    public.register_live_object_blob(text, text, bigint, text, text, text),
    public.gc_claim_object(text, text, text, timestamptz, timestamptz),
    public.gc_record_delete_failure(text, text, text, timestamptz, text),
    public.gc_complete_object_delete(text, text, text, timestamptz),
    public.observe_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint),
    public.claim_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text, bigint),
    public.orphan_claim_is_current(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text),
    public.complete_unregistered_object_delete(text, text, text),
    public.record_unregistered_object_delete_failure(text, text, text, text),
    public.enqueue_job(text, text, text, text, jsonb, timestamptz),
    public.claim_job(text[], text, interval),
    public.heartbeat_job(text, text, bigint, interval),
    public.lock_active_job_lease(text, text, text, bigint),
    public.complete_job(text, text, bigint, jsonb),
    public.fail_job(text, text, bigint, text, timestamptz),
    public.park_job(text, text, bigint, text),
    public.publish_tracking_input_snapshot(jsonb)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION
    public.register_live_object_blob(text, text, bigint, text, text, text)
TO leo_capture, leo_analysis;
GRANT EXECUTE ON FUNCTION
    public.gc_claim_object(text, text, text, timestamptz, timestamptz),
    public.gc_record_delete_failure(text, text, text, timestamptz, text),
    public.gc_complete_object_delete(text, text, text, timestamptz),
    public.observe_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint),
    public.claim_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text, bigint),
    public.orphan_claim_is_current(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text),
    public.complete_unregistered_object_delete(text, text, text),
    public.record_unregistered_object_delete_failure(text, text, text, text)
TO leo_maintenance;
GRANT EXECUTE ON FUNCTION
    public.enqueue_job(text, text, text, text, jsonb, timestamptz),
    public.claim_job(text[], text, interval),
    public.heartbeat_job(text, text, bigint, interval),
    public.lock_active_job_lease(text, text, text, bigint),
    public.complete_job(text, text, bigint, jsonb),
    public.fail_job(text, text, bigint, text, timestamptz),
    public.park_job(text, text, bigint, text),
    public.publish_tracking_input_snapshot(jsonb)
TO leo_analysis;

CREATE OR REPLACE FUNCTION public.enqueue_job(
    p_job_id text,
    p_job_type text,
    p_payload_schema_id text,
    p_payload_schema_version text,
    p_payload jsonb,
    p_available_at_utc timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE inserted_id text;
BEGIN
    INSERT INTO public.job
        (job_id, job_type, payload_schema_id, payload_schema_version,
         payload, state, available_at_utc)
    VALUES
        (p_job_id, p_job_type, p_payload_schema_id, p_payload_schema_version,
         p_payload, 'ready', p_available_at_utc)
    ON CONFLICT (job_id) DO NOTHING
    RETURNING job_id INTO inserted_id;
    RETURN inserted_id IS NOT NULL;
END
$function$;

CREATE OR REPLACE FUNCTION public.claim_job(
    p_job_types text[],
    p_lease_token text,
    p_ttl_interval interval
) RETURNS SETOF public.job
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.cardinality(p_job_types) = 0
       OR p_lease_token IS NULL OR p_lease_token = ''
       OR p_ttl_interval <= interval '0 seconds' THEN
        RAISE EXCEPTION 'invalid job claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT j.job_id
          FROM public.job AS j
         WHERE j.job_type = ANY(p_job_types)
           AND j.available_at_utc <= pg_catalog.clock_timestamp()
           AND (
               j.state IN ('ready', 'failed')
               OR (
                   j.state = 'leased'
                   AND j.lease_expires_utc <= pg_catalog.clock_timestamp()
               )
           )
         ORDER BY j.available_at_utc, j.job_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    )
    UPDATE public.job AS j
       SET state = 'leased',
           attempt = j.attempt + 1,
           lease_generation = j.lease_generation + 1,
           lease_token = p_lease_token,
           lease_expires_utc = pg_catalog.clock_timestamp() + p_ttl_interval
      FROM candidate
     WHERE j.job_id = candidate.job_id
     RETURNING j.*;
END
$function$;

CREATE OR REPLACE FUNCTION public.heartbeat_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_ttl_interval interval
) RETURNS SETOF public.job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.job
       SET lease_expires_utc = pg_catalog.clock_timestamp() + p_ttl_interval
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > pg_catalog.clock_timestamp()
       AND p_ttl_interval > interval '0 seconds'
     RETURNING *;
$function$;

CREATE OR REPLACE FUNCTION public.lock_active_job_lease(
    p_job_id text,
    p_job_type text,
    p_lease_token text,
    p_lease_generation bigint
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1
      FROM public.job
     WHERE job_id = p_job_id
       AND job_type = p_job_type
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > pg_catalog.clock_timestamp()
     FOR UPDATE;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION public.complete_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_result_ref jsonb
) RETURNS SETOF public.job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.job
       SET state = 'succeeded', result_ref = p_result_ref,
           lease_token = NULL, lease_expires_utc = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > pg_catalog.clock_timestamp()
       AND p_result_ref IS NOT NULL
     RETURNING *;
$function$;

CREATE OR REPLACE FUNCTION public.fail_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_reason text,
    p_retry_at_utc timestamptz
) RETURNS SETOF public.job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.job
       SET state = 'failed', last_error = p_reason,
           available_at_utc = p_retry_at_utc,
           lease_token = NULL, lease_expires_utc = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > pg_catalog.clock_timestamp()
       AND p_reason IS NOT NULL AND p_reason <> ''
     RETURNING *;
$function$;

CREATE OR REPLACE FUNCTION public.park_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_reason text
) RETURNS SETOF public.job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.job
       SET state = 'parked',
           park_reason = p_reason,
           parked_at_utc = pg_catalog.clock_timestamp(),
           lease_token = NULL,
           lease_expires_utc = NULL,
           result_ref = NULL,
           last_error = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > pg_catalog.clock_timestamp()
       AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
     RETURNING *;
$function$;

CREATE OR REPLACE FUNCTION public.observe_unregistered_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    blob_state text;
    existing public.object_orphan_observation%ROWTYPE;
    observed_at timestamptz := pg_catalog.clock_timestamp();
    evidence_matches boolean;
BEGIN
    IF p_digest_algorithm <> 'sha256'
       OR p_digest_value !~ '^[0-9a-f]{64}$'
       OR p_byte_count < 0
       OR p_locator <> 'cas:sha256:' || p_digest_value
       OR p_filesystem_device < 0
       OR p_filesystem_inode < 0
       OR p_filesystem_parent_device < 0
       OR p_filesystem_parent_inode < 0
       OR p_filesystem_mtime_ns < 0 THEN
        RAISE EXCEPTION 'invalid orphan inventory evidence';
    END IF;
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);

    SELECT lifecycle_state INTO blob_state
      FROM public.object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    IF FOUND THEN
        RETURN CASE blob_state
            WHEN 'live' THEN 'live'
            WHEN 'gc_deleted' THEN 'tombstone'
            WHEN 'gc_claimed' THEN 'in_flight'
            ELSE 'registered'
        END;
    END IF;

    SELECT * INTO existing
      FROM public.object_orphan_observation
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.object_orphan_observation
            (digest_algorithm, digest_value, byte_count, locator,
             filesystem_device, filesystem_inode,
             filesystem_parent_device, filesystem_parent_inode,
             filesystem_mtime_ns,
             first_observed_at, last_observed_at, state)
        VALUES
            (p_digest_algorithm, p_digest_value, p_byte_count, p_locator,
             p_filesystem_device, p_filesystem_inode,
             p_filesystem_parent_device, p_filesystem_parent_inode,
             p_filesystem_mtime_ns,
             observed_at, observed_at, 'observed');
        INSERT INTO public.object_orphan_event
            (digest_algorithm, digest_value, event, event_at)
        VALUES (p_digest_algorithm, p_digest_value, 'observed', observed_at);
        RETURN 'unregistered';
    END IF;

    IF existing.state = 'claimed' THEN
        RETURN 'in_flight';
    END IF;
    evidence_matches := existing.byte_count = p_byte_count
        AND existing.locator = p_locator
        AND existing.filesystem_device = p_filesystem_device
        AND existing.filesystem_inode = p_filesystem_inode
        AND existing.filesystem_parent_device = p_filesystem_parent_device
        AND existing.filesystem_parent_inode = p_filesystem_parent_inode
        AND existing.filesystem_mtime_ns = p_filesystem_mtime_ns;
    IF evidence_matches AND existing.state = 'observed' THEN
        UPDATE public.object_orphan_observation
           SET last_observed_at = observed_at
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value;
        RETURN 'unregistered';
    END IF;

    UPDATE public.object_orphan_observation
       SET byte_count = p_byte_count,
           locator = p_locator,
           filesystem_device = p_filesystem_device,
           filesystem_inode = p_filesystem_inode,
           filesystem_parent_device = p_filesystem_parent_device,
           filesystem_parent_inode = p_filesystem_parent_inode,
           filesystem_mtime_ns = p_filesystem_mtime_ns,
           first_observed_at = observed_at,
           last_observed_at = observed_at,
           state = 'observed',
           claim_token = NULL,
           claimed_at = NULL,
           deleted_at = NULL,
           registered_at = NULL
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    INSERT INTO public.object_orphan_event
        (digest_algorithm, digest_value, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, 'evidence_changed', observed_at);
    RETURN 'unregistered';
END
$function$;

CREATE OR REPLACE FUNCTION public.claim_unregistered_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint,
    p_claim_token text,
    p_minimum_age_seconds bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    existing public.object_orphan_observation%ROWTYPE;
    v_claimed_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_claim_token IS NULL OR p_claim_token = ''
       OR p_minimum_age_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid orphan claim';
    END IF;
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    IF EXISTS (
        SELECT 1 FROM public.object_blob
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
    ) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO existing
      FROM public.object_orphan_observation
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF existing.state = 'claimed' THEN
        IF existing.byte_count = p_byte_count
           AND existing.locator = p_locator
           AND existing.filesystem_device = p_filesystem_device
           AND existing.filesystem_inode = p_filesystem_inode
           AND existing.filesystem_parent_device = p_filesystem_parent_device
           AND existing.filesystem_parent_inode = p_filesystem_parent_inode
           AND existing.filesystem_mtime_ns = p_filesystem_mtime_ns THEN
            RETURN existing.claim_token;
        END IF;
        RETURN NULL;
    END IF;
    IF existing.state <> 'observed'
       OR existing.byte_count <> p_byte_count
       OR existing.locator <> p_locator
       OR existing.filesystem_device <> p_filesystem_device
       OR existing.filesystem_inode <> p_filesystem_inode
       OR existing.filesystem_parent_device <> p_filesystem_parent_device
       OR existing.filesystem_parent_inode <> p_filesystem_parent_inode
       OR existing.filesystem_mtime_ns <> p_filesystem_mtime_ns
       OR existing.first_observed_at
            + pg_catalog.make_interval(secs => p_minimum_age_seconds) > v_claimed_at THEN
        RETURN NULL;
    END IF;
    UPDATE public.object_orphan_observation
       SET state = 'claimed', claim_token = p_claim_token,
           claimed_at = v_claimed_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    INSERT INTO public.object_orphan_event
        (digest_algorithm, digest_value, event, event_at, claim_token)
    VALUES (p_digest_algorithm, p_digest_value, 'claimed',
            v_claimed_at, p_claim_token);
    RETURN p_claim_token;
END
$function$;

CREATE OR REPLACE FUNCTION public.orphan_claim_is_current(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint,
    p_claim_token text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    RETURN NOT EXISTS (
        SELECT 1 FROM public.object_blob
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
    ) AND EXISTS (
        SELECT 1 FROM public.object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed'
           AND claim_token = p_claim_token
           AND byte_count = p_byte_count
           AND locator = p_locator
           AND filesystem_device = p_filesystem_device
           AND filesystem_inode = p_filesystem_inode
           AND filesystem_parent_device = p_filesystem_parent_device
           AND filesystem_parent_inode = p_filesystem_parent_inode
           AND filesystem_mtime_ns = p_filesystem_mtime_ns
         FOR UPDATE
    );
END
$function$;

CREATE OR REPLACE FUNCTION public.complete_unregistered_object_delete(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE completed_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    UPDATE public.object_orphan_observation
       SET state = 'deleted', claim_token = NULL, claimed_at = NULL,
           deleted_at = completed_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND state = 'claimed' AND claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.object_orphan_event
        (digest_algorithm, digest_value, event, event_at, claim_token)
    VALUES (p_digest_algorithm, p_digest_value, 'deleted', completed_at, p_claim_token);
    RETURN true;
END
$function$;

CREATE OR REPLACE FUNCTION public.record_unregistered_object_delete_failure(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_detail text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_detail IS NULL OR p_detail = '' OR pg_catalog.length(p_detail) > 1000 THEN
        RAISE EXCEPTION 'invalid orphan delete failure detail';
    END IF;
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    IF NOT EXISTS (
        SELECT 1 FROM public.object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed' AND claim_token = p_claim_token
         FOR UPDATE
    ) THEN RETURN false; END IF;
    INSERT INTO public.object_orphan_event
        (digest_algorithm, digest_value, event, claim_token, detail)
    VALUES (p_digest_algorithm, p_digest_value, 'delete_failed',
            p_claim_token, p_detail)
    ON CONFLICT (digest_algorithm, digest_value, event, claim_token)
    DO NOTHING;
    RETURN true;
END
$function$;

CREATE OR REPLACE FUNCTION public.object_digest_fence(
    p_digest_algorithm text, p_digest_value text
) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_digest_algorithm || ':' || p_digest_value, 0)
    );
$function$;

CREATE OR REPLACE FUNCTION public.register_live_object_blob(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_media_type text,
    p_format_id text,
    p_locator text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_registered_at timestamptz := pg_catalog.clock_timestamp();
    registered public.object_blob%ROWTYPE;
BEGIN
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    IF EXISTS (
        SELECT 1 FROM public.object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed'
    ) THEN
        RAISE EXCEPTION 'object has an active orphan-deletion claim'
            USING ERRCODE = '40001';
    END IF;

    INSERT INTO public.object_blob
        (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
    VALUES
        (p_digest_algorithm, p_digest_value, p_byte_count,
         p_media_type, p_format_id, p_locator)
    ON CONFLICT DO NOTHING;

    UPDATE public.object_blob
       SET lifecycle_state = 'live',
           gc_deleted_at = NULL,
           gc_last_error = NULL,
           verified_at = NULL,
           registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_deleted'
       AND byte_count = p_byte_count
       AND media_type = p_media_type
       AND format_id = p_format_id
       AND locator = p_locator;

    SELECT * INTO registered
      FROM public.object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR KEY SHARE;
    IF NOT FOUND
       OR registered.lifecycle_state <> 'live'
       OR registered.byte_count <> p_byte_count
       OR registered.media_type <> p_media_type
       OR registered.format_id <> p_format_id
       OR registered.locator <> p_locator THEN
        RETURN;
    END IF;

    UPDATE public.object_orphan_observation
       SET state = 'registered', claim_token = NULL, claimed_at = NULL,
           deleted_at = NULL, registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND state <> 'registered';
    IF FOUND THEN
        INSERT INTO public.object_orphan_event
            (digest_algorithm, digest_value, event, event_at)
        VALUES (p_digest_algorithm, p_digest_value, 'registered', v_registered_at);
    END IF;
END
$function$;

CREATE OR REPLACE FUNCTION public.gc_claim_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz,
    p_claim_expires_at timestamptz
) RETURNS SETOF public.object_blob
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    candidate public.object_blob%ROWTYPE;
    status public.object_retention_status%ROWTYPE;
BEGIN
    IF p_claim_token IS NULL OR p_claim_token = '' OR p_claim_expires_at <= p_now THEN
        RAISE EXCEPTION 'invalid GC claim';
    END IF;
    SELECT * INTO candidate
      FROM public.object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND (lifecycle_state IN ('live', 'gc_delete_failed')
            OR (lifecycle_state = 'gc_claimed' AND gc_claim_expires_at <= p_now))
     FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;

    SELECT * INTO status
      FROM public.object_retention_status
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    IF status.policy_count = 0
       OR NOT status.all_policies_allow_delete
       OR status.eligible_after > p_now
       OR status.live_reference_count <> 0 THEN
        RETURN;
    END IF;

    UPDATE public.object_blob
       SET lifecycle_state = 'gc_claimed',
           gc_claim_token = p_claim_token,
           gc_claimed_at = p_now,
           gc_claim_expires_at = p_claim_expires_at,
           gc_last_error = NULL
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     RETURNING * INTO candidate;
    INSERT INTO public.object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token, 'claimed', p_now);
    RETURN NEXT candidate;
END
$function$;

CREATE OR REPLACE FUNCTION public.gc_record_delete_failure(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz,
    p_detail text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_detail IS NULL OR p_detail = '' OR pg_catalog.length(p_detail) > 1000 THEN
        RAISE EXCEPTION 'invalid GC failure detail';
    END IF;
    UPDATE public.object_blob
       SET lifecycle_state = 'gc_delete_failed',
           gc_claim_token = NULL,
           gc_claimed_at = NULL,
           gc_claim_expires_at = NULL,
           gc_last_error = p_detail
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_claimed'
       AND gc_claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at, detail)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token,
            'delete_failed', p_now, p_detail);
    RETURN true;
END
$function$;

CREATE OR REPLACE FUNCTION public.gc_complete_object_delete(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.object_blob
       SET lifecycle_state = 'gc_deleted',
           gc_claim_token = NULL,
           gc_claimed_at = NULL,
           gc_claim_expires_at = NULL,
           gc_last_error = NULL,
           gc_deleted_at = p_now
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_claimed'
       AND gc_claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token, 'deleted', p_now);
    RETURN true;
END
$function$;

COMMIT;
