BEGIN;

ALTER TABLE job
    DROP CONSTRAINT job_state_check,
    ADD COLUMN park_reason text,
    ADD COLUMN parked_at_utc timestamptz,
    ADD CONSTRAINT job_state_check CHECK (
        state IN ('ready', 'leased', 'failed', 'succeeded', 'parked')
    ),
    ADD CONSTRAINT job_parked_state_consistent CHECK (
        (
            state = 'parked'
            AND park_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
            AND parked_at_utc IS NOT NULL
            AND result_ref IS NULL
            AND last_error IS NULL
        )
        OR
        (
            state <> 'parked'
            AND park_reason IS NULL
            AND parked_at_utc IS NULL
        )
    );

CREATE FUNCTION enqueue_job(
    p_job_id text,
    p_job_type text,
    p_payload_schema_id text,
    p_payload_schema_version text,
    p_payload jsonb,
    p_available_at_utc timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE inserted_id text;
BEGIN
    INSERT INTO job
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

CREATE FUNCTION claim_job(
    p_job_types text[],
    p_lease_token text,
    p_ttl_interval interval
) RETURNS SETOF job
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF cardinality(p_job_types) = 0
       OR p_lease_token IS NULL OR p_lease_token = ''
       OR p_ttl_interval <= interval '0 seconds' THEN
        RAISE EXCEPTION 'invalid job claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT j.job_id
          FROM job j
         WHERE j.job_type = ANY(p_job_types)
           AND j.available_at_utc <= clock_timestamp()
           AND (
               j.state IN ('ready', 'failed')
               OR (
                   j.state = 'leased'
                   AND j.lease_expires_utc <= clock_timestamp()
               )
           )
         ORDER BY j.available_at_utc, j.job_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    )
    UPDATE job j
       SET state = 'leased',
           attempt = j.attempt + 1,
           lease_generation = j.lease_generation + 1,
           lease_token = p_lease_token,
           lease_expires_utc = clock_timestamp() + p_ttl_interval
      FROM candidate
     WHERE j.job_id = candidate.job_id
     RETURNING j.*;
END
$function$;

CREATE FUNCTION heartbeat_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_ttl_interval interval
) RETURNS SETOF job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    UPDATE job
       SET lease_expires_utc = clock_timestamp() + p_ttl_interval
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > clock_timestamp()
       AND p_ttl_interval > interval '0 seconds'
     RETURNING *;
$function$;

CREATE FUNCTION lock_active_job_lease(
    p_job_id text,
    p_job_type text,
    p_lease_token text,
    p_lease_generation bigint
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    PERFORM 1
      FROM job
     WHERE job_id = p_job_id
       AND job_type = p_job_type
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > clock_timestamp()
     FOR UPDATE;
    RETURN FOUND;
END
$function$;

CREATE FUNCTION complete_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_result_ref jsonb
) RETURNS SETOF job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    UPDATE job
       SET state = 'succeeded', result_ref = p_result_ref,
           lease_token = NULL, lease_expires_utc = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > clock_timestamp()
       AND p_result_ref IS NOT NULL
     RETURNING *;
$function$;

CREATE FUNCTION fail_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_reason text,
    p_retry_at_utc timestamptz
) RETURNS SETOF job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    UPDATE job
       SET state = 'failed', last_error = p_reason,
           available_at_utc = p_retry_at_utc,
           lease_token = NULL, lease_expires_utc = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > clock_timestamp()
       AND p_reason IS NOT NULL AND p_reason <> ''
     RETURNING *;
$function$;

CREATE FUNCTION park_job(
    p_job_id text,
    p_lease_token text,
    p_lease_generation bigint,
    p_reason text
) RETURNS SETOF job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    UPDATE job
       SET state = 'parked',
           park_reason = p_reason,
           parked_at_utc = clock_timestamp(),
           lease_token = NULL,
           lease_expires_utc = NULL,
           result_ref = NULL,
           last_error = NULL
     WHERE job_id = p_job_id
       AND state = 'leased'
       AND lease_token = p_lease_token
       AND lease_generation = p_lease_generation
       AND lease_expires_utc > clock_timestamp()
       AND p_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
     RETURNING *;
$function$;

-- A future operator-owned maintenance migration may add a separately
-- authenticated requeue function. No current runtime or maintenance role can
-- directly mutate a parked row back into the claimable state machine.
REVOKE INSERT, UPDATE ON job FROM leo_analysis;

REVOKE ALL ON FUNCTION enqueue_job(text, text, text, text, jsonb, timestamptz)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION claim_job(text[], text, interval)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION heartbeat_job(text, text, bigint, interval)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION lock_active_job_lease(text, text, text, bigint)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION complete_job(text, text, bigint, jsonb)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION fail_job(text, text, bigint, text, timestamptz)
    FROM PUBLIC, leo_capture, leo_dashboard;
REVOKE ALL ON FUNCTION park_job(text, text, bigint, text)
    FROM PUBLIC, leo_capture, leo_dashboard;

GRANT EXECUTE ON FUNCTION enqueue_job(text, text, text, text, jsonb, timestamptz),
    claim_job(text[], text, interval),
    heartbeat_job(text, text, bigint, interval),
    lock_active_job_lease(text, text, text, bigint),
    complete_job(text, text, bigint, jsonb),
    fail_job(text, text, bigint, text, timestamptz),
    park_job(text, text, bigint, text)
TO leo_analysis;

COMMIT;
