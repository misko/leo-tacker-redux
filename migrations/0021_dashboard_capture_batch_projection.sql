BEGIN;

-- Immutable dashboard-owned snapshots.  They deliberately contain all facts
-- needed by the V2 read port so the dashboard never follows capture, job, CAS,
-- or constructed storage paths.
CREATE TABLE public.dashboard_capture_batch_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('public.dashboard_projection_sequence'),
    schema_id text NOT NULL
        CHECK (schema_id = 'org.leo-flow.dashboard.capture-batch'),
    schema_version text NOT NULL CHECK (schema_version = '0.1'),
    batch_id text NOT NULL
        CHECK (batch_id ~ '^cbatch_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    capture_revision smallint NOT NULL CHECK (capture_revision BETWEEN 0 AND 2),
    mode text NOT NULL CHECK (mode IN ('independent', 'coordinated')),
    coordination_claim text NOT NULL CHECK (
        coordination_claim IN ('none', 'measured_software_coordination')),
    requested_start_utc_ns bigint NOT NULL CHECK (requested_start_utc_ns >= 0),
    requested_start_skew_ns bigint NOT NULL CHECK (requested_start_skew_ns >= 0),
    observed_start_skew_ns bigint CHECK (observed_start_skew_ns >= 0),
    maximum_observed_start_skew_ns bigint
        CHECK (maximum_observed_start_skew_ns >= 0),
    paired_analysis_eligibility text NOT NULL
        CHECK (paired_analysis_eligibility IN ('pending', 'eligible', 'ineligible')),
    semantic_view jsonb NOT NULL CHECK (jsonb_typeof(semantic_view) = 'object'),
    projected_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (batch_id, projection_sequence),
    CHECK (
        (mode = 'independent'
         AND coordination_claim = 'none'
         AND maximum_observed_start_skew_ns IS NULL)
        OR
        (mode = 'coordinated'
         AND coordination_claim = 'measured_software_coordination'
         AND requested_start_skew_ns = 0
         AND maximum_observed_start_skew_ns IS NOT NULL)
    )
);

CREATE INDEX dashboard_capture_batch_recent_idx
    ON public.dashboard_capture_batch_projection
       (requested_start_utc_ns DESC, batch_id DESC, projection_sequence DESC);

CREATE INDEX dashboard_capture_batch_identity_idx
    ON public.dashboard_capture_batch_projection
       (batch_id, projection_sequence DESC);

CREATE TABLE public.dashboard_capture_attempt_projection (
    projection_sequence bigint NOT NULL REFERENCES
        public.dashboard_capture_batch_projection(projection_sequence),
    attempt_position smallint NOT NULL CHECK (attempt_position IN (0, 1)),
    attempt_id text NOT NULL
        CHECK (attempt_id ~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    radio_id text NOT NULL
        CHECK (radio_id ~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    plan_id text NOT NULL
        CHECK (plan_id ~ '^plan_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    requested_start_utc_ns bigint NOT NULL CHECK (requested_start_utc_ns >= 0),
    capture_state text NOT NULL
        CHECK (capture_state IN ('pending', 'succeeded', 'failed')),
    observed_start_utc_ns bigint CHECK (observed_start_utc_ns >= 0),
    recording_id text
        CHECK (recording_id ~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    failure_reason text
        CHECK (failure_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'),
    analysis_state text NOT NULL
        CHECK (analysis_state IN ('unavailable', 'pending', 'running', 'complete', 'failed')),
    analysis_result_available boolean NOT NULL,
    PRIMARY KEY (projection_sequence, attempt_position),
    UNIQUE (projection_sequence, attempt_id),
    UNIQUE (projection_sequence, radio_id),
    UNIQUE (projection_sequence, plan_id),
    UNIQUE (projection_sequence, recording_id),
    CHECK (
        (capture_state = 'pending'
         AND observed_start_utc_ns IS NULL
         AND recording_id IS NULL
         AND failure_reason IS NULL
         AND analysis_state = 'unavailable'
         AND analysis_result_available IS FALSE)
        OR
        (capture_state = 'failed'
         AND recording_id IS NULL
         AND failure_reason IS NOT NULL
         AND analysis_state = 'unavailable'
         AND analysis_result_available IS FALSE)
        OR
        (capture_state = 'succeeded'
         AND observed_start_utc_ns IS NOT NULL
         AND recording_id IS NOT NULL
         AND failure_reason IS NULL
         AND analysis_state <> 'unavailable'
         AND (analysis_result_available IS FALSE OR analysis_state = 'complete'))
    )
);

CREATE FUNCTION public.publish_dashboard_capture_batch(p_view jsonb)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    top_key_count integer;
    attempt_key_count integer;
    attempt jsonb;
    attempt_position bigint;
    target_batch_id text;
    mode text;
    claim text;
    eligibility text;
    revision smallint;
    requested_skew bigint;
    observed_skew bigint;
    maximum_skew bigint;
    requested_starts bigint[] := ARRAY[]::bigint[];
    observed_starts bigint[] := ARRAY[]::bigint[];
    attempt_ids text[] := ARRAY[]::text[];
    radio_ids text[] := ARRAY[]::text[];
    plan_ids text[] := ARRAY[]::text[];
    recording_ids text[] := ARRAY[]::text[];
    terminal_count integer := 0;
    success_count integer := 0;
    previous_attempt_id text;
    observed_start bigint;
    recording_id text;
    failure_reason text;
    analysis_state text;
    capture_state text;
    result_available boolean;
    latest_sequence bigint;
    latest_view jsonb;
    prior_attempt jsonb;
    inserted_sequence bigint;
    invoking_role text := pg_catalog.current_setting('role', true);
    stale_capture_replay boolean := false;
    other_analysis_change boolean := false;
BEGIN
    IF p_view IS NULL OR pg_catalog.jsonb_typeof(p_view) <> 'object' THEN
        RAISE EXCEPTION 'invalid dashboard capture batch projection'
            USING ERRCODE = '22023';
    END IF;
    SELECT pg_catalog.count(*) INTO top_key_count
      FROM pg_catalog.jsonb_object_keys(p_view);
    IF top_key_count <> 10 OR NOT p_view ?& ARRAY[
        'schema', 'batch_id', 'mode', 'coordination_claim', 'attempts',
        'revision', 'requested_start_skew_ns', 'observed_start_skew_ns',
        'maximum_observed_start_skew_ns', 'paired_analysis_eligibility'] THEN
        RAISE EXCEPTION 'invalid dashboard capture batch projection keys'
            USING ERRCODE = '22023';
    END IF;
    IF pg_catalog.jsonb_typeof(p_view->'schema') <> 'object'
       OR (SELECT pg_catalog.count(*) FROM
              pg_catalog.jsonb_object_keys(p_view->'schema')) <> 2
       OR p_view #>> '{schema,schema_id}' <>
          'org.leo-flow.dashboard.capture-batch'
       OR pg_catalog.jsonb_typeof(p_view #> '{schema,version}') <> 'object'
       OR (SELECT pg_catalog.count(*) FROM
              pg_catalog.jsonb_object_keys(p_view #> '{schema,version}')) <> 2
       OR p_view #>> '{schema,version,major}' <> '0'
       OR p_view #>> '{schema,version,minor}' <> '1' THEN
        RAISE EXCEPTION 'unsupported dashboard capture batch projection schema'
            USING ERRCODE = '22023';
    END IF;

    target_batch_id := p_view->>'batch_id';
    mode := p_view->>'mode';
    claim := p_view->>'coordination_claim';
    eligibility := p_view->>'paired_analysis_eligibility';
    revision := (p_view->>'revision')::smallint;
    requested_skew := (p_view->>'requested_start_skew_ns')::bigint;
    observed_skew := (p_view->>'observed_start_skew_ns')::bigint;
    maximum_skew := (p_view->>'maximum_observed_start_skew_ns')::bigint;
    IF target_batch_id !~ '^cbatch_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR mode NOT IN ('independent', 'coordinated')
       OR claim NOT IN ('none', 'measured_software_coordination')
       OR eligibility NOT IN ('pending', 'eligible', 'ineligible')
       OR revision NOT BETWEEN 0 AND 2
       OR requested_skew < 0
       OR observed_skew < 0
       OR maximum_skew < 0
       OR pg_catalog.jsonb_typeof(p_view->'attempts') <> 'array'
       OR pg_catalog.jsonb_array_length(p_view->'attempts') <> 2 THEN
        RAISE EXCEPTION 'invalid dashboard capture batch projection values'
            USING ERRCODE = '22023';
    END IF;

    FOR attempt, attempt_position IN
        SELECT item.value, item.ordinality - 1
          FROM pg_catalog.jsonb_array_elements(p_view->'attempts')
               WITH ORDINALITY AS item(value, ordinality)
         ORDER BY item.ordinality
    LOOP
        IF pg_catalog.jsonb_typeof(attempt) <> 'object' THEN
            RAISE EXCEPTION 'invalid dashboard capture attempt projection'
                USING ERRCODE = '22023';
        END IF;
        SELECT pg_catalog.count(*) INTO attempt_key_count
          FROM pg_catalog.jsonb_object_keys(attempt);
        IF attempt_key_count <> 10 OR NOT attempt ?& ARRAY[
            'attempt_id', 'radio_id', 'plan_id', 'requested_start_utc_ns',
            'capture_state', 'observed_start_utc_ns', 'recording_id',
            'failure_reason', 'analysis_state', 'analysis_result_available'] THEN
            RAISE EXCEPTION 'invalid dashboard capture attempt projection keys'
                USING ERRCODE = '22023';
        END IF;
        IF attempt->>'attempt_id' !~
               '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR attempt->>'radio_id' !~
               '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR attempt->>'plan_id' !~
               '^plan_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR (attempt->>'requested_start_utc_ns')::bigint < 0
           OR attempt->>'capture_state' NOT IN ('pending', 'succeeded', 'failed')
           OR attempt->>'analysis_state' NOT IN
              ('unavailable', 'pending', 'running', 'complete', 'failed')
           OR pg_catalog.jsonb_typeof(attempt->'analysis_result_available') <>
              'boolean' THEN
            RAISE EXCEPTION 'invalid dashboard capture attempt projection values'
                USING ERRCODE = '22023';
        END IF;
        capture_state := attempt->>'capture_state';
        observed_start := (attempt->>'observed_start_utc_ns')::bigint;
        recording_id := attempt->>'recording_id';
        failure_reason := attempt->>'failure_reason';
        analysis_state := attempt->>'analysis_state';
        result_available := (attempt->>'analysis_result_available')::boolean;
        IF observed_start < 0
           OR (recording_id IS NOT NULL AND recording_id !~
               '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$')
           OR (failure_reason IS NOT NULL AND failure_reason !~
               '^[a-z0-9][a-z0-9._:-]{0,127}$') THEN
            RAISE EXCEPTION 'invalid dashboard capture attempt evidence'
                USING ERRCODE = '22023';
        END IF;
        IF capture_state = 'pending' AND
           (observed_start IS NOT NULL OR recording_id IS NOT NULL
            OR failure_reason IS NOT NULL OR analysis_state <> 'unavailable'
            OR result_available) THEN
            RAISE EXCEPTION 'pending capture has terminal dashboard evidence'
                USING ERRCODE = '22023';
        ELSIF capture_state = 'failed' AND
              (recording_id IS NOT NULL OR failure_reason IS NULL
               OR analysis_state <> 'unavailable' OR result_available) THEN
            RAISE EXCEPTION 'failed capture has invalid dashboard evidence'
                USING ERRCODE = '22023';
        ELSIF capture_state = 'succeeded' AND
              (observed_start IS NULL OR recording_id IS NULL
               OR failure_reason IS NOT NULL OR analysis_state = 'unavailable'
               OR (result_available AND analysis_state <> 'complete')) THEN
            RAISE EXCEPTION 'successful capture has invalid dashboard evidence'
                USING ERRCODE = '22023';
        END IF;
        IF previous_attempt_id IS NOT NULL
           AND previous_attempt_id >= attempt->>'attempt_id' THEN
            RAISE EXCEPTION 'dashboard capture attempts are not canonical'
                USING ERRCODE = '22023';
        END IF;
        previous_attempt_id := attempt->>'attempt_id';
        attempt_ids := pg_catalog.array_append(attempt_ids, attempt->>'attempt_id');
        radio_ids := pg_catalog.array_append(radio_ids, attempt->>'radio_id');
        plan_ids := pg_catalog.array_append(plan_ids, attempt->>'plan_id');
        requested_starts := pg_catalog.array_append(
            requested_starts, (attempt->>'requested_start_utc_ns')::bigint);
        observed_starts := pg_catalog.array_append(observed_starts, observed_start);
        IF recording_id IS NOT NULL THEN
            recording_ids := pg_catalog.array_append(recording_ids, recording_id);
        END IF;
        IF capture_state <> 'pending' THEN terminal_count := terminal_count + 1; END IF;
        IF capture_state = 'succeeded' THEN success_count := success_count + 1; END IF;
    END LOOP;

    IF (SELECT pg_catalog.count(DISTINCT item) FROM
            pg_catalog.unnest(attempt_ids) AS values(item)) <> 2
       OR (SELECT pg_catalog.count(DISTINCT item) FROM
               pg_catalog.unnest(radio_ids) AS values(item)) <> 2
       OR (SELECT pg_catalog.count(DISTINCT item) FROM
               pg_catalog.unnest(plan_ids) AS values(item)) <> 2
       OR (SELECT pg_catalog.count(DISTINCT item) FROM
               pg_catalog.unnest(recording_ids) AS values(item)) <>
          pg_catalog.array_length(recording_ids, 1)
       OR revision <> terminal_count
       OR requested_skew <> pg_catalog.abs(requested_starts[1] - requested_starts[2])
       OR observed_skew IS DISTINCT FROM
          (CASE WHEN observed_starts[1] IS NOT NULL
                     AND observed_starts[2] IS NOT NULL
               THEN pg_catalog.abs(observed_starts[1] - observed_starts[2])
               ELSE NULL END) THEN
        RAISE EXCEPTION 'dashboard capture batch evidence is inconsistent'
            USING ERRCODE = '22023';
    END IF;
    IF mode = 'independent' AND
       (claim <> 'none' OR maximum_skew IS NOT NULL) THEN
        RAISE EXCEPTION 'independent dashboard capture makes no timing claim'
            USING ERRCODE = '22023';
    ELSIF mode = 'coordinated' AND
          (claim <> 'measured_software_coordination'
           OR requested_skew <> 0 OR maximum_skew IS NULL) THEN
        RAISE EXCEPTION 'coordinated dashboard capture has invalid timing evidence'
            USING ERRCODE = '22023';
    END IF;
    IF eligibility <> (CASE
        WHEN terminal_count < 2 THEN 'pending'
        WHEN success_count < 2 THEN 'ineligible'
        WHEN mode = 'coordinated'
             AND (observed_skew IS NULL OR observed_skew > maximum_skew)
            THEN 'ineligible'
        ELSE 'eligible' END) THEN
        RAISE EXCEPTION 'dashboard paired-analysis eligibility is inconsistent'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(target_batch_id, 0));
    SELECT projection_sequence, semantic_view
      INTO latest_sequence, latest_view
      FROM public.dashboard_capture_batch_projection
     WHERE batch_id = target_batch_id
     ORDER BY projection_sequence DESC
     LIMIT 1;
    IF latest_sequence IS NOT NULL AND latest_view = p_view THEN
        RETURN latest_sequence;
    END IF;
    IF latest_sequence IS NOT NULL THEN
        IF latest_view->'mode' <> p_view->'mode'
           OR latest_view->'coordination_claim' <> p_view->'coordination_claim'
           OR latest_view->'requested_start_skew_ns' <>
              p_view->'requested_start_skew_ns'
           OR latest_view->'maximum_observed_start_skew_ns' <>
              p_view->'maximum_observed_start_skew_ns'
           OR (latest_view->>'revision')::smallint > revision THEN
            RAISE EXCEPTION 'capture batch projection rewrites immutable intent'
                USING ERRCODE = '23505';
        END IF;
        IF (latest_view->>'revision')::smallint = 2
           AND (latest_view->'observed_start_skew_ns' <>
                    p_view->'observed_start_skew_ns'
                OR latest_view->'paired_analysis_eligibility' <>
                    p_view->'paired_analysis_eligibility') THEN
            RAISE EXCEPTION 'terminal batch projection rewrites capture semantics'
                USING ERRCODE = '23505';
        END IF;
        FOR attempt, attempt_position IN
            SELECT item.value, item.ordinality - 1
              FROM pg_catalog.jsonb_array_elements(p_view->'attempts')
                   WITH ORDINALITY AS item(value, ordinality)
             ORDER BY item.ordinality
        LOOP
            prior_attempt := latest_view #>
                ARRAY['attempts', attempt_position::text];
            IF prior_attempt->'attempt_id' <> attempt->'attempt_id'
               OR prior_attempt->'radio_id' <> attempt->'radio_id'
               OR prior_attempt->'plan_id' <> attempt->'plan_id'
               OR prior_attempt->'requested_start_utc_ns' <>
                  attempt->'requested_start_utc_ns'
               OR (prior_attempt->>'capture_state') <> 'pending' AND
                  (prior_attempt->'capture_state' <> attempt->'capture_state'
                   OR prior_attempt->'observed_start_utc_ns' <>
                      attempt->'observed_start_utc_ns'
                   OR prior_attempt->'recording_id' <> attempt->'recording_id'
                   OR prior_attempt->'failure_reason' <> attempt->'failure_reason') THEN
                RAISE EXCEPTION 'capture batch projection rewrites attempt evidence'
                    USING ERRCODE = '23505';
            END IF;
            IF prior_attempt->'analysis_state' <> attempt->'analysis_state'
               OR prior_attempt->'analysis_result_available' <>
                  attempt->'analysis_result_available' THEN
                IF prior_attempt->>'analysis_state' = 'complete'
                   AND (prior_attempt->>'analysis_result_available')::boolean
                   AND invoking_role IN ('leo_capture', 'leo_analysis')
                   AND attempt->>'analysis_state' = 'pending'
                   AND NOT (attempt->>'analysis_result_available')::boolean THEN
                    stale_capture_replay := true;
                ELSIF prior_attempt->>'analysis_state' = 'complete'
                      AND (prior_attempt->>'analysis_result_available')::boolean THEN
                    RAISE EXCEPTION 'available analysis result cannot regress'
                        USING ERRCODE = '23505';
                ELSE
                    other_analysis_change := true;
                END IF;
            END IF;
        END LOOP;
        IF stale_capture_replay AND other_analysis_change THEN
            RAISE EXCEPTION 'stale capture replay contains analysis changes'
                USING ERRCODE = '23505';
        ELSIF stale_capture_replay THEN
            RETURN latest_sequence;
        END IF;
    END IF;

    INSERT INTO public.dashboard_capture_batch_projection(
        schema_id, schema_version, batch_id, capture_revision, mode,
        coordination_claim, requested_start_utc_ns, requested_start_skew_ns,
        observed_start_skew_ns, maximum_observed_start_skew_ns,
        paired_analysis_eligibility, semantic_view)
    VALUES (
        'org.leo-flow.dashboard.capture-batch', '0.1', target_batch_id, revision, mode,
        claim, least(requested_starts[1], requested_starts[2]),
        requested_skew, observed_skew, maximum_skew, eligibility, p_view)
    RETURNING projection_sequence INTO inserted_sequence;

    FOR attempt, attempt_position IN
        SELECT item.value, item.ordinality - 1
          FROM pg_catalog.jsonb_array_elements(p_view->'attempts')
               WITH ORDINALITY AS item(value, ordinality)
         ORDER BY item.ordinality
    LOOP
        INSERT INTO public.dashboard_capture_attempt_projection(
            projection_sequence, attempt_position, attempt_id, radio_id, plan_id,
            requested_start_utc_ns, capture_state, observed_start_utc_ns,
            recording_id, failure_reason, analysis_state,
            analysis_result_available)
        VALUES (
            inserted_sequence, attempt_position, attempt->>'attempt_id',
            attempt->>'radio_id', attempt->>'plan_id',
            (attempt->>'requested_start_utc_ns')::bigint,
            attempt->>'capture_state',
            (attempt->>'observed_start_utc_ns')::bigint,
            attempt->>'recording_id', attempt->>'failure_reason',
            attempt->>'analysis_state',
            (attempt->>'analysis_result_available')::boolean);
    END LOOP;
    RETURN inserted_sequence;
END
$function$;

CREATE FUNCTION public.resolve_dashboard_capture_batches_for_recording(
    p_recording_id text)
RETURNS TABLE(batch_id text, semantic_view jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_recording_id !~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$' THEN
        RAISE EXCEPTION 'invalid dashboard recording identity'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH latest_batch AS (
        SELECT DISTINCT ON (projection.batch_id)
               projection.projection_sequence,
               projection.batch_id,
               projection.semantic_view
          FROM public.dashboard_capture_batch_projection AS projection
         ORDER BY projection.batch_id, projection.projection_sequence DESC
    )
    SELECT latest.batch_id, latest.semantic_view
      FROM latest_batch AS latest
      JOIN public.dashboard_capture_attempt_projection AS attempt
        ON attempt.projection_sequence = latest.projection_sequence
     WHERE attempt.recording_id = p_recording_id
     ORDER BY latest.batch_id;
END
$function$;

CREATE FUNCTION public.capture_analysis_drain_ready()
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
                WHERE pending_job.job_type = 'recording_analysis'
                  AND pending_job.state IN ('ready', 'leased', 'failed')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.feature_projection_work AS pending_projection
                WHERE pending_projection.state IN ('ready', 'leased', 'failed')
           )
      INTO is_ready;
    RETURN is_ready;
END
$function$;

GRANT SELECT, INSERT ON public.dashboard_capture_batch_projection,
    public.dashboard_capture_attempt_projection TO leo_routine_owner;
GRANT SELECT ON public.dashboard_recording_projection TO leo_routine_owner;
GRANT USAGE, SELECT ON SEQUENCE public.dashboard_projection_sequence
TO leo_routine_owner;

ALTER FUNCTION public.publish_dashboard_capture_batch(jsonb)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.resolve_dashboard_capture_batches_for_recording(text)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_analysis_drain_ready()
    OWNER TO leo_routine_owner;

REVOKE ALL ON public.dashboard_capture_batch_projection,
    public.dashboard_capture_attempt_projection
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.publish_dashboard_capture_batch(jsonb)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.resolve_dashboard_capture_batches_for_recording(text)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.capture_analysis_drain_ready()
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT SELECT ON public.dashboard_capture_batch_projection,
    public.dashboard_capture_attempt_projection TO leo_dashboard;
GRANT EXECUTE ON FUNCTION public.publish_dashboard_capture_batch(jsonb)
TO leo_capture, leo_analysis;
GRANT EXECUTE ON FUNCTION public.resolve_dashboard_capture_batches_for_recording(text)
TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.capture_analysis_drain_ready()
TO leo_capture;

COMMIT;
