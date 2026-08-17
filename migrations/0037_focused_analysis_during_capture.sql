BEGIN;

-- One terminal synchronized focused dwell contains exactly one batch, two
-- recordings, and the six jobs submitted by the three approved analysis
-- lanes.  It is deliberately separate from the exact 36-batch campaign-window
-- contract introduced by migration 0032.
CREATE TABLE public.focused_analysis_pair_scope (
    scope_digest text PRIMARY KEY CHECK (scope_digest ~ '^sha256:[0-9a-f]{64}$'),
    capture_definition_digest text NOT NULL CHECK (
        capture_definition_digest ~ '^sha256:[0-9a-f]{64}$'),
    batch_id text NOT NULL UNIQUE,
    recording_ids text[] NOT NULL CHECK (cardinality(recording_ids) = 2),
    recording_digests text[] NOT NULL CHECK (cardinality(recording_digests) = 2),
    source_job_ids text[] NOT NULL CHECK (cardinality(source_job_ids) = 6),
    registered_at_utc timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.focused_analysis_pair_job_scope (
    source_job_id text PRIMARY KEY REFERENCES public.job(job_id),
    scope_digest text NOT NULL REFERENCES public.focused_analysis_pair_scope(scope_digest),
    job_type text NOT NULL CHECK (job_type IN (
        'recording_analysis', 'waterfall_analysis', 'starlink_suite_analysis'
    )),
    recording_id text NOT NULL
);

CREATE FUNCTION public.register_focused_analysis_pair_scope_v1(
    p_capture_definition_digest text,
    p_scope_digest text,
    p_batch_id text,
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
    IF p_capture_definition_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_scope_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_batch_id IS NULL OR p_batch_id = ''
       OR cardinality(p_recording_ids) <> 2
       OR cardinality(p_recording_digests) <> 2
       OR cardinality(p_feature_job_ids) <> 2
       OR cardinality(p_waterfall_job_ids) <> 2
       OR cardinality(p_starlink_suite_job_ids) <> 2
       OR (SELECT count(DISTINCT value) FROM unnest(p_recording_ids) AS value) <> 2
       OR (SELECT count(DISTINCT value) FROM unnest(p_recording_digests) AS value) <> 2
       OR (SELECT count(DISTINCT value) FROM unnest(all_job_ids) AS value) <> 6
    THEN
        RAISE EXCEPTION 'invalid focused analysis pair scope'
            USING ERRCODE='22023';
    END IF;

    IF EXISTS (
        SELECT 1
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
          LEFT JOIN public.job AS j ON j.job_id=expected.id
         WHERE j.job_id IS NULL
            OR j.job_type <> expected.expected_type
            OR j.payload ->> 'recording_id' <> expected.recording_id
    ) THEN
        RAISE EXCEPTION 'focused analysis job scope contradicts durable job'
            USING ERRCODE='23514';
    END IF;

    IF NOT EXISTS (
        WITH latest AS (
            SELECT max(p.projection_sequence) AS projection_sequence
              FROM public.dashboard_capture_batch_projection AS p
             WHERE p.batch_id=p_batch_id
        )
        SELECT 1
          FROM latest AS l
          JOIN public.dashboard_capture_batch_projection AS b
            ON b.projection_sequence=l.projection_sequence
         WHERE b.batch_id=p_batch_id
           AND b.capture_revision=2
           AND b.paired_analysis_eligibility='eligible'
           AND (
               SELECT count(*)
                 FROM public.dashboard_capture_attempt_projection AS a
                WHERE a.projection_sequence=l.projection_sequence
                  AND a.capture_state='succeeded'
                  AND a.recording_id=ANY(p_recording_ids)
           )=2
           AND NOT EXISTS (
               SELECT 1
                 FROM public.dashboard_capture_attempt_projection AS a
                WHERE a.projection_sequence=l.projection_sequence
                  AND (a.capture_state<>'succeeded'
                       OR a.recording_id<>ALL(p_recording_ids)))
    ) THEN
        RAISE EXCEPTION 'focused analysis scope lacks terminal capture evidence'
            USING ERRCODE='23514';
    END IF;

    INSERT INTO public.focused_analysis_pair_scope(
        scope_digest,capture_definition_digest,batch_id,recording_ids,
        recording_digests,source_job_ids)
    VALUES (
        p_scope_digest,p_capture_definition_digest,p_batch_id,p_recording_ids,
        p_recording_digests,all_job_ids)
    ON CONFLICT DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM public.focused_analysis_pair_scope AS s
         WHERE s.scope_digest=p_scope_digest
           AND s.capture_definition_digest=p_capture_definition_digest
           AND s.batch_id=p_batch_id
           AND s.recording_ids=p_recording_ids
           AND s.recording_digests=p_recording_digests
           AND s.source_job_ids=all_job_ids
    ) THEN
        RAISE EXCEPTION 'focused analysis pair scope identity conflict'
            USING ERRCODE='23505';
    END IF;

    INSERT INTO public.focused_analysis_pair_job_scope(
        source_job_id,scope_digest,job_type,recording_id)
    SELECT id,p_scope_digest,expected_type,recording_id
      FROM (
        SELECT id, 'recording_analysis'::text AS expected_type, recording_id
          FROM unnest(p_feature_job_ids,p_recording_ids) AS x(id,recording_id)
        UNION ALL
        SELECT id, 'waterfall_analysis', recording_id
          FROM unnest(p_waterfall_job_ids,p_recording_ids) AS x(id,recording_id)
        UNION ALL
        SELECT id, 'starlink_suite_analysis', recording_id
          FROM unnest(p_starlink_suite_job_ids,p_recording_ids) AS x(id,recording_id)
      ) AS expected
    ON CONFLICT DO NOTHING;

    SELECT count(*) INTO registered_count
      FROM public.focused_analysis_pair_job_scope AS s
     WHERE s.scope_digest=p_scope_digest AND s.source_job_id=ANY(all_job_ids);
    IF registered_count <> 6 THEN
        RAISE EXCEPTION 'focused analysis pair job scope identity conflict'
            USING ERRCODE='23505';
    END IF;
    RETURN true;
END $function$;

CREATE FUNCTION public.capture_registered_analysis_safe_v3(
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
                   WHERE s.source_job_id=j.job_id)
              AND NOT EXISTS (
                  SELECT 1 FROM public.focused_analysis_pair_job_scope AS s
                   WHERE s.source_job_id=j.job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.feature_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id)
              AND NOT EXISTS (
                  SELECT 1 FROM public.focused_analysis_pair_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.waterfall_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id)
              AND NOT EXISTS (
                  SELECT 1 FROM public.focused_analysis_pair_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id))
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp())
       AND NOT EXISTS (
           SELECT 1 FROM public.starlink_detector_suite_projection_work AS w
            WHERE w.state='leased' AND w.lease_expires_utc>clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM public.campaign_analysis_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id)
              AND NOT EXISTS (
                  SELECT 1 FROM public.focused_analysis_pair_job_scope AS s
                   WHERE s.source_job_id=w.source_job_id));
$function$;

ALTER TABLE public.focused_analysis_pair_scope OWNER TO leo_routine_owner;
ALTER TABLE public.focused_analysis_pair_job_scope OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.focused_analysis_pair_scope,
    public.focused_analysis_pair_job_scope TO leo_routine_owner;

ALTER FUNCTION public.register_focused_analysis_pair_scope_v1(
    text,text,text,text[],text[],text[],text[],text[]) OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_registered_analysis_safe_v3(text)
    OWNER TO leo_routine_owner;

REVOKE ALL ON TABLE public.focused_analysis_pair_scope,
    public.focused_analysis_pair_job_scope
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
REVOKE ALL ON FUNCTION public.register_focused_analysis_pair_scope_v1(
    text,text,text,text[],text[],text[],text[],text[]),
    public.capture_registered_analysis_safe_v3(text)
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.register_focused_analysis_pair_scope_v1(
    text,text,text,text[],text[],text[],text[],text[]) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.capture_registered_analysis_safe_v3(text)
    TO leo_capture;

COMMIT;
