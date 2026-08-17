BEGIN;

-- Exact terminal-recording scopes are registered before an online worker may
-- lease work.  The table remains private; capture and analysis use only the
-- narrow SECURITY DEFINER ports below.
CREATE TABLE public.campaign_analysis_window_scope (
    definition_digest text NOT NULL,
    window_digest text NOT NULL,
    first_success_index integer NOT NULL CHECK (
        first_success_index >= 0 AND first_success_index % 36 = 0
    ),
    batch_ids text[] NOT NULL CHECK (cardinality(batch_ids) = 36),
    recording_ids text[] NOT NULL CHECK (cardinality(recording_ids) = 72),
    recording_digests text[] NOT NULL CHECK (cardinality(recording_digests) = 72),
    source_job_ids text[] NOT NULL CHECK (cardinality(source_job_ids) = 216),
    registered_at_utc timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (definition_digest, first_success_index),
    UNIQUE (window_digest)
);

CREATE TABLE public.campaign_analysis_job_scope (
    source_job_id text PRIMARY KEY REFERENCES public.job(job_id),
    definition_digest text NOT NULL,
    window_digest text NOT NULL REFERENCES
        public.campaign_analysis_window_scope(window_digest),
    job_type text NOT NULL CHECK (job_type IN (
        'recording_analysis', 'waterfall_analysis', 'starlink_suite_analysis'
    )),
    recording_id text NOT NULL
);

CREATE INDEX campaign_analysis_job_scope_definition_idx
    ON public.campaign_analysis_job_scope(definition_digest, source_job_id);

CREATE FUNCTION public.register_campaign_analysis_window_scope_v1(
    p_definition_digest text,
    p_window_digest text,
    p_first_success_index integer,
    p_batch_ids text[],
    p_recording_ids text[],
    p_recording_digests text[],
    p_feature_job_ids text[],
    p_waterfall_job_ids text[],
    p_starlink_suite_job_ids text[])
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE
    all_job_ids text[];
    registered_count integer;
BEGIN
    all_job_ids := p_feature_job_ids || p_waterfall_job_ids ||
                   p_starlink_suite_job_ids;
    IF p_definition_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_window_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_first_success_index IS NULL OR p_first_success_index < 0
       OR p_first_success_index % 36 <> 0
       OR cardinality(p_batch_ids) <> 36
       OR cardinality(p_recording_ids) <> 72
       OR cardinality(p_recording_digests) <> 72
       OR cardinality(p_feature_job_ids) <> 72
       OR cardinality(p_waterfall_job_ids) <> 72
       OR cardinality(p_starlink_suite_job_ids) <> 72
       OR (SELECT count(DISTINCT value) FROM unnest(p_batch_ids) AS value) <> 36
       OR (SELECT count(DISTINCT value) FROM unnest(p_recording_ids) AS value) <> 72
       OR (SELECT count(DISTINCT value) FROM unnest(all_job_ids) AS value) <> 216
    THEN
        RAISE EXCEPTION 'invalid campaign analysis window scope'
            USING ERRCODE='22023';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM (
            SELECT id, 'recording_analysis'::text AS expected_type, recording_id
              FROM unnest(p_feature_job_ids, p_recording_ids)
                   AS x(id, recording_id)
            UNION ALL
            SELECT id, 'waterfall_analysis', recording_id
              FROM unnest(p_waterfall_job_ids, p_recording_ids)
                   AS x(id, recording_id)
            UNION ALL
            SELECT id, 'starlink_suite_analysis', recording_id
              FROM unnest(p_starlink_suite_job_ids, p_recording_ids)
                   AS x(id, recording_id)
          ) AS expected
          LEFT JOIN public.job AS j ON j.job_id=expected.id
         WHERE j.job_id IS NULL
            OR j.job_type <> expected.expected_type
            OR j.payload ->> 'recording_id' <> expected.recording_id
    ) THEN
        RAISE EXCEPTION 'campaign analysis job scope contradicts durable job'
            USING ERRCODE='23514';
    END IF;

    -- Independently prove that every requested batch's latest public capture
    -- projection is terminal with exactly the two recording IDs in this
    -- window.  Possession of the analysis role alone cannot bless in-flight
    -- or foreign recordings into a capture-safe scope.
    IF EXISTS (
        WITH expected_batch AS (
            SELECT batch_id, ordinal
              FROM unnest(p_batch_ids) WITH ORDINALITY AS b(batch_id, ordinal)
        ), latest AS (
            SELECT e.batch_id, e.ordinal,
                   (SELECT max(p.projection_sequence)
                      FROM public.dashboard_capture_batch_projection AS p
                     WHERE p.batch_id=e.batch_id) AS projection_sequence
              FROM expected_batch AS e
        ), observed AS (
            SELECT l.batch_id, l.ordinal, l.projection_sequence,
                   count(a.*) FILTER (WHERE a.capture_state='succeeded') AS successes,
                   array_agg(a.recording_id ORDER BY a.recording_id)
                       FILTER (WHERE a.capture_state='succeeded') AS recording_ids
              FROM latest AS l
              LEFT JOIN public.dashboard_capture_attempt_projection AS a
                ON a.projection_sequence=l.projection_sequence
             GROUP BY l.batch_id,l.ordinal,l.projection_sequence
        )
        SELECT 1 FROM observed AS o
         WHERE o.projection_sequence IS NULL
            OR o.successes <> 2
            OR o.recording_ids <> ARRAY[
                least(
                    p_recording_ids[(o.ordinal::integer-1)*2+1],
                    p_recording_ids[(o.ordinal::integer-1)*2+2]),
                greatest(
                    p_recording_ids[(o.ordinal::integer-1)*2+1],
                    p_recording_ids[(o.ordinal::integer-1)*2+2])]
    ) THEN
        RAISE EXCEPTION 'campaign analysis scope lacks terminal capture evidence'
            USING ERRCODE='23514';
    END IF;

    INSERT INTO public.campaign_analysis_window_scope(
        definition_digest, window_digest, first_success_index, batch_ids,
        recording_ids, recording_digests, source_job_ids)
    VALUES (
        p_definition_digest, p_window_digest, p_first_success_index, p_batch_ids,
        p_recording_ids, p_recording_digests, all_job_ids)
    ON CONFLICT DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM public.campaign_analysis_window_scope AS s
         WHERE s.definition_digest=p_definition_digest
           AND s.window_digest=p_window_digest
           AND s.first_success_index=p_first_success_index
           AND s.batch_ids=p_batch_ids
           AND s.recording_ids=p_recording_ids
           AND s.recording_digests=p_recording_digests
           AND s.source_job_ids=all_job_ids
    ) THEN
        RAISE EXCEPTION 'campaign analysis window scope identity conflict'
            USING ERRCODE='23505';
    END IF;

    INSERT INTO public.campaign_analysis_job_scope(
        source_job_id, definition_digest, window_digest, job_type, recording_id)
    SELECT id, p_definition_digest, p_window_digest, expected_type, recording_id
      FROM (
        SELECT id, 'recording_analysis'::text AS expected_type, recording_id
          FROM unnest(p_feature_job_ids, p_recording_ids) AS x(id, recording_id)
        UNION ALL
        SELECT id, 'waterfall_analysis', recording_id
          FROM unnest(p_waterfall_job_ids, p_recording_ids) AS x(id, recording_id)
        UNION ALL
        SELECT id, 'starlink_suite_analysis', recording_id
          FROM unnest(p_starlink_suite_job_ids, p_recording_ids) AS x(id, recording_id)
      ) AS expected
    ON CONFLICT DO NOTHING;

    SELECT count(*) INTO registered_count
      FROM public.campaign_analysis_job_scope AS s
     WHERE s.definition_digest=p_definition_digest
       AND s.window_digest=p_window_digest
       AND s.source_job_id=ANY(all_job_ids);
    IF registered_count <> 216 THEN
        RAISE EXCEPTION 'campaign analysis job scope identity conflict'
            USING ERRCODE='23505';
    END IF;
    RETURN true;
END $function$;

CREATE FUNCTION public.capture_campaign_analysis_safe_v1(p_definition_digest text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    SELECT p_definition_digest ~ '^sha256:[0-9a-f]{64}$'
       AND NOT EXISTS (
           SELECT 1 FROM public.job AS j
            WHERE j.state='leased'
              AND j.job_type IN (
                  'recording_analysis','model_analysis','waterfall_analysis',
                  'starlink_analysis','starlink_suite_analysis')
              AND j.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=j.job_id
                     AND s.definition_digest=p_definition_digest))
       AND NOT EXISTS (
           SELECT 1 FROM public.feature_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id
                     AND s.definition_digest=p_definition_digest))
       AND NOT EXISTS (
           SELECT 1 FROM public.waterfall_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id
                     AND s.definition_digest=p_definition_digest))
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp())
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_detector_suite_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id
                     AND s.definition_digest=p_definition_digest));
$function$;

ALTER TABLE public.campaign_analysis_window_scope OWNER TO leo_routine_owner;
ALTER TABLE public.campaign_analysis_job_scope OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.campaign_analysis_window_scope,
    public.campaign_analysis_job_scope TO leo_routine_owner;

ALTER FUNCTION public.register_campaign_analysis_window_scope_v1(
    text,text,integer,text[],text[],text[],text[],text[],text[])
OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_campaign_analysis_safe_v1(text)
OWNER TO leo_routine_owner;

REVOKE ALL ON TABLE public.campaign_analysis_window_scope,
    public.campaign_analysis_job_scope
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
REVOKE ALL ON FUNCTION public.register_campaign_analysis_window_scope_v1(
    text,text,integer,text[],text[],text[],text[],text[],text[]),
    public.capture_campaign_analysis_safe_v1(text)
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.register_campaign_analysis_window_scope_v1(
    text,text,integer,text[],text[],text[],text[],text[],text[])
TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.capture_campaign_analysis_safe_v1(text)
TO leo_capture;

COMMIT;
