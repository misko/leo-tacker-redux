BEGIN;

CREATE TABLE public.recording_starlink_candidate (
    analysis_id text PRIMARY KEY
        CHECK (analysis_id ~ '^slanalysis_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    input_recording_digest_algorithm text NOT NULL
        CHECK (input_recording_digest_algorithm = 'sha256'),
    input_recording_digest_value text NOT NULL
        CHECK (input_recording_digest_value ~ '^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm = 'sha256'),
    request_digest_value text NOT NULL CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm = 'sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    candidate_count integer NOT NULL CHECK (candidate_count BETWEEN 1 AND 64),
    analyzed_stream_count integer NOT NULL
        CHECK (analyzed_stream_count BETWEEN 1 AND 64
               AND analyzed_stream_count = candidate_count),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recording_id, input_recording_digest_algorithm,
            input_recording_digest_value, request_digest_algorithm,
            request_digest_value),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm, digest_value)
);

CREATE TRIGGER recording_starlink_candidate_bundle_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value
ON public.recording_starlink_candidate
FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');

CREATE OR REPLACE VIEW public.object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,
           data_digest_value AS digest_value,
           'recording.data'::text AS reference_kind,
           recording_id::text AS owner_id FROM public.recording
UNION ALL SELECT metadata_digest_algorithm, metadata_digest_value,
           'recording.metadata', recording_id::text FROM public.recording
UNION ALL SELECT raw_digest_algorithm, raw_digest_value,
           'ephemeris_snapshot.raw', snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT normalized_digest_algorithm, normalized_digest_value,
           'ephemeris_snapshot.normalized', snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT provenance_digest_algorithm, provenance_digest_value,
           'ephemeris_snapshot.provenance', snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'dataset_snapshot.bundle', snapshot_id::text FROM public.dataset_snapshot
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'feature_set.bundle', feature_set_id::text FROM public.feature_set
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'model_snapshot.bundle', model_snapshot_id::text FROM public.model_snapshot
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'hardware_snapshot.bundle', snapshot_id::text FROM public.hardware_snapshot
UNION ALL SELECT report_digest_algorithm, report_digest_value,
           'detector_evaluation_report.report', evaluation_id::text
           FROM public.detector_evaluation_report
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'tracking_input_snapshot.bundle', snapshot_id::text
           FROM public.tracking_input_snapshot
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'tracking_model_snapshot.bundle', model_run_id::text
           FROM public.tracking_model_snapshot
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'recording_waterfall.bundle', product_id::text
           FROM public.recording_waterfall
UNION ALL SELECT bundle_digest_algorithm, bundle_digest_value,
           'recording_starlink_candidate.bundle', analysis_id::text
           FROM public.recording_starlink_candidate;

CREATE TABLE public.starlink_projection_work (
    work_id text PRIMARY KEY CHECK (work_id ~ '^slwork_[0-9a-f]{64}$'),
    source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    analysis_id text NOT NULL UNIQUE
        REFERENCES public.recording_starlink_candidate(analysis_id),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm = 'sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
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
    CHECK ((state = 'leased' AND lease_token IS NOT NULL
            AND lease_expires_utc IS NOT NULL)
           OR (state <> 'leased' AND lease_token IS NULL
               AND lease_expires_utc IS NULL)),
    CHECK ((state = 'parked'
            AND park_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
            AND parked_at_utc IS NOT NULL)
           OR (state <> 'parked' AND park_reason IS NULL
               AND parked_at_utc IS NULL)),
    CHECK ((state = 'succeeded' AND projected_at_utc IS NOT NULL)
           OR (state <> 'succeeded' AND projected_at_utc IS NULL))
);

CREATE INDEX starlink_projection_work_claim_idx
ON public.starlink_projection_work(available_at_utc, work_id)
WHERE state IN ('ready', 'failed', 'leased');

CREATE TABLE public.dashboard_recording_starlink_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('public.dashboard_projection_sequence'),
    recording_id text NOT NULL
        CHECK (recording_id ~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    analysis_id text NOT NULL
        CHECK (analysis_id ~ '^slanalysis_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    bundle_digest_value text NOT NULL
        CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    semantic_view jsonb NOT NULL CHECK (jsonb_typeof(semantic_view) = 'object'),
    projected_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recording_id, analysis_id, bundle_digest_value)
);

CREATE INDEX dashboard_recording_starlink_latest_idx
ON public.dashboard_recording_starlink_projection(
    recording_id, projection_sequence DESC);

GRANT SELECT, INSERT ON public.recording_starlink_candidate,
    public.starlink_projection_work,
    public.dashboard_recording_starlink_projection TO leo_routine_owner;
GRANT UPDATE ON public.starlink_projection_work TO leo_routine_owner;
GRANT SELECT ON public.recording_starlink_candidate TO leo_analysis;
GRANT SELECT ON public.dashboard_recording_starlink_projection TO leo_dashboard;
REVOKE ALL ON public.starlink_projection_work
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
    public.recording_starlink_candidate,
    public.dashboard_recording_starlink_projection
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

CREATE FUNCTION public.publish_recording_starlink_candidate(
    p_analysis_id text, p_recording_id text,
    p_input_digest_algorithm text, p_input_digest_value text,
    p_request_digest_algorithm text, p_request_digest_value text,
    p_bundle_digest_algorithm text, p_bundle_digest_value text,
    p_candidate_count integer, p_analyzed_stream_count integer,
    p_idempotency_key text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH inserted AS (
        INSERT INTO public.recording_starlink_candidate(
            analysis_id, recording_id,
            input_recording_digest_algorithm, input_recording_digest_value,
            request_digest_algorithm, request_digest_value,
            bundle_digest_algorithm, bundle_digest_value,
            candidate_count, analyzed_stream_count, idempotency_key)
        VALUES (p_analysis_id, p_recording_id,
            p_input_digest_algorithm, p_input_digest_value,
            p_request_digest_algorithm, p_request_digest_value,
            p_bundle_digest_algorithm, p_bundle_digest_value,
            p_candidate_count, p_analyzed_stream_count, p_idempotency_key)
        ON CONFLICT DO NOTHING RETURNING analysis_id)
    SELECT pg_catalog.count(*) = 1 FROM inserted;
$function$;

CREATE FUNCTION public.publish_starlink_projection_work(
    p_work_id text, p_source_job_id text, p_source_lease_token text,
    p_source_lease_generation bigint, p_analysis_id text,
    p_recording_id text, p_bundle_digest_algorithm text,
    p_bundle_digest_value text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE inserted_id text;
BEGIN
    IF p_work_id !~ '^slwork_[0-9a-f]{64}$'
       OR p_source_lease_token IS NULL OR p_source_lease_token = ''
       OR p_source_lease_generation <= 0 THEN
        RAISE EXCEPTION 'invalid Starlink projection work publication'
            USING ERRCODE = '22023';
    END IF;
    PERFORM 1 FROM public.job AS j
     WHERE j.job_id = p_source_job_id AND j.job_type = 'starlink_analysis'
       AND j.state = 'leased' AND j.lease_token = p_source_lease_token
       AND j.lease_generation = p_source_lease_generation
       AND j.lease_expires_utc > pg_catalog.clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Starlink source lease is not current'
            USING ERRCODE = '55000';
    END IF;
    PERFORM 1 FROM public.recording_starlink_candidate AS s
      JOIN public.object_blob AS o
        ON (o.digest_algorithm,o.digest_value)=
           (s.bundle_digest_algorithm,s.bundle_digest_value)
     WHERE s.analysis_id=p_analysis_id AND s.recording_id=p_recording_id
       AND (s.bundle_digest_algorithm,s.bundle_digest_value)=
           (p_bundle_digest_algorithm,p_bundle_digest_value)
       AND o.lifecycle_state='live';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Starlink projection source is not authoritative'
            USING ERRCODE = '23503';
    END IF;
    INSERT INTO public.starlink_projection_work(
        work_id,source_job_id,analysis_id,recording_id,
        bundle_digest_algorithm,bundle_digest_value)
    VALUES (p_work_id,p_source_job_id,p_analysis_id,p_recording_id,
            p_bundle_digest_algorithm,p_bundle_digest_value)
    ON CONFLICT DO NOTHING RETURNING work_id INTO inserted_id;
    IF inserted_id IS NULL AND NOT EXISTS (
        SELECT 1 FROM public.starlink_projection_work
         WHERE work_id=p_work_id AND source_job_id=p_source_job_id
           AND analysis_id=p_analysis_id AND recording_id=p_recording_id
           AND bundle_digest_algorithm=p_bundle_digest_algorithm
           AND bundle_digest_value=p_bundle_digest_value) THEN
        RAISE EXCEPTION 'Starlink projection work identity conflict'
            USING ERRCODE = '23505';
    END IF;
    RETURN true;
END $function$;

CREATE FUNCTION public.claim_starlink_projection_work(
    p_lease_token text, p_ttl interval)
RETURNS SETOF public.starlink_projection_work
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lease_token IS NULL OR p_lease_token='' OR p_ttl<=interval '0' THEN
        RAISE EXCEPTION 'invalid Starlink projection claim' USING ERRCODE='22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT w.work_id FROM public.starlink_projection_work AS w
         WHERE w.available_at_utc<=pg_catalog.clock_timestamp()
           AND (w.state IN ('ready','failed') OR
                (w.state='leased' AND
                 w.lease_expires_utc<=pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc,w.work_id
         FOR UPDATE SKIP LOCKED LIMIT 1)
    UPDATE public.starlink_projection_work AS w
       SET state='leased',attempt=w.attempt+1,
           lease_generation=w.lease_generation+1,lease_token=p_lease_token,
           lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl,last_error=NULL
      FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*;
END $function$;

CREATE FUNCTION public.complete_starlink_projection_work(
    p_work_id text,p_lease_token text,p_generation bigint)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    WITH changed AS (UPDATE public.starlink_projection_work
       SET state='succeeded',projected_at_utc=pg_catalog.clock_timestamp(),
           lease_token=NULL,lease_expires_utc=NULL,last_error=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation
       AND lease_expires_utc>pg_catalog.clock_timestamp()
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.retry_starlink_projection_work(
    p_work_id text,p_lease_token text,p_generation bigint,
    p_reason text,p_delay interval)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    WITH changed AS (UPDATE public.starlink_projection_work
       SET state='failed',last_error=p_reason,
           available_at_utc=pg_catalog.clock_timestamp()+p_delay,
           lease_token=NULL,lease_expires_utc=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation
       AND lease_expires_utc>pg_catalog.clock_timestamp()
       AND p_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$'
       AND p_delay>interval '0'
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.park_starlink_projection_work(
    p_work_id text,p_lease_token text,p_generation bigint,p_reason text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    WITH changed AS (UPDATE public.starlink_projection_work
       SET state='parked',park_reason=p_reason,
           parked_at_utc=pg_catalog.clock_timestamp(),
           lease_token=NULL,lease_expires_utc=NULL,last_error=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation
       AND lease_expires_utc>pg_catalog.clock_timestamp()
       AND p_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$'
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.publish_dashboard_recording_starlink(
    p_view jsonb, p_work_id text, p_lease_token text,
    p_lease_generation bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE
    target_recording text;
    target_analysis text;
    target_digest text;
    target_count integer;
    candidate jsonb;
    reason jsonb;
    existing_sequence bigint;
    inserted_sequence bigint;
BEGIN
    PERFORM 1 FROM public.starlink_projection_work AS work
     WHERE work.work_id=p_work_id AND work.state='leased'
       AND work.lease_token=p_lease_token
       AND work.lease_generation=p_lease_generation
       AND work.lease_expires_utc>pg_catalog.clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Starlink projection lease is not current'
            USING ERRCODE='55000';
    END IF;
    IF p_view IS NULL OR pg_catalog.jsonb_typeof(p_view)<>'object'
       OR pg_catalog.octet_length(p_view::text)>1048576
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_view))<>3
       OR NOT p_view ?& ARRAY['schema','decision','candidates']
       OR p_view#>>'{schema,schema_id}'<>
          'org.leo-flow.dashboard.recording-starlink-candidates'
       OR p_view#>>'{schema,version,major}'<>'0'
       OR p_view#>>'{schema,version,minor}'<>'1'
       OR pg_catalog.jsonb_typeof(p_view->'decision')<>'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
              p_view->'decision'))<>8
       OR NOT (p_view->'decision') ?& ARRAY[
          'schema','recording_id','state','analyzed_stream_count',
          'search_candidate_count','calibrated_detection_count',
          'analysis_ref','reason_codes']
       OR p_view#>>'{decision,schema,schema_id}'<>
          'org.leo-flow.dashboard.recording-starlink-decision'
       OR p_view#>>'{decision,schema,version,major}'<>'0'
       OR p_view#>>'{decision,schema,version,minor}'<>'1'
       OR pg_catalog.jsonb_typeof(p_view#>'{decision,analysis_ref}')<>'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
              p_view#>'{decision,analysis_ref}'))<>3
       OR NOT (p_view#>'{decision,analysis_ref}') ?& ARRAY[
          'artifact_id','digest','schema']
       OR pg_catalog.jsonb_typeof(p_view#>'{decision,reason_codes}')<>'array'
       OR pg_catalog.jsonb_typeof(p_view->'candidates')<>'array' THEN
        RAISE EXCEPTION 'invalid dashboard Starlink projection'
            USING ERRCODE='22023';
    END IF;
    target_recording:=p_view#>>'{decision,recording_id}';
    target_analysis:=p_view#>>'{decision,analysis_ref,artifact_id}';
    target_digest:=p_view#>>'{decision,analysis_ref,digest,value}';
    target_count:=(p_view#>>'{decision,search_candidate_count}')::integer;
    IF target_recording!~'^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR target_analysis!~'^slanalysis_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR target_digest!~'^[0-9a-f]{64}$'
       OR p_view#>>'{decision,state}'<>'candidates'
       OR p_view#>'{decision,calibrated_detection_count}'<>'null'::jsonb
       OR (p_view#>>'{decision,analyzed_stream_count}')::integer<>target_count
       OR target_count NOT BETWEEN 1 AND 64
       OR pg_catalog.jsonb_array_length(p_view->'candidates')<>target_count
       OR p_view#>>'{decision,analysis_ref,digest,algorithm}'<>'sha256'
       OR p_view#>>'{decision,analysis_ref,schema,schema_id}'<>
          'org.leo-flow.starlink-pilot-analysis-bundle' THEN
        RAISE EXCEPTION 'invalid uncalibrated Starlink candidate state'
            USING ERRCODE='22023';
    END IF;
    FOR candidate IN SELECT value FROM pg_catalog.jsonb_array_elements(
        p_view->'candidates') LOOP
        IF pg_catalog.jsonb_typeof(candidate)<>'object'
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(candidate))<>13
           OR NOT candidate ?& ARRAY[
             'candidate_id','segment_id','receiver_chain_id','edge',
             'search_identity_digest','winning_epoch_sample','winning_cfo_hz',
             'search_cell_count','frame_support','exact_score',
             'conditioned_control_score','exact_minus_control_margin',
             'pss_evidence_status']
           OR candidate->>'candidate_id'!~'^slcandidate_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR candidate->>'segment_id'!~'^seg_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR candidate->>'receiver_chain_id'!~'^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR candidate->>'edge' NOT IN ('lower','upper')
           OR candidate#>>'{search_identity_digest,algorithm}'<>'sha256'
           OR candidate#>>'{search_identity_digest,value}'!~'^[0-9a-f]{64}$'
           OR (candidate->>'winning_epoch_sample')::bigint<0
           OR (candidate->>'search_cell_count')::integer<=0
           OR (candidate->>'frame_support')::integer<=0
           OR (candidate->>'winning_cfo_hz')::double precision IN
              ('NaN'::double precision,'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (candidate->>'exact_score')::double precision IN
              ('NaN'::double precision,'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (candidate->>'conditioned_control_score')::double precision IN
              ('NaN'::double precision,'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (candidate->>'exact_minus_control_margin')::double precision IN
              ('NaN'::double precision,'Infinity'::double precision,
               '-Infinity'::double precision)
           OR candidate->>'pss_evidence_status' NOT IN ('not_evaluated','evaluated')
        THEN
            RAISE EXCEPTION 'invalid dashboard Starlink candidate'
                USING ERRCODE='22023';
        END IF;
    END LOOP;
    FOR reason IN SELECT value FROM pg_catalog.jsonb_array_elements(
        p_view#>'{decision,reason_codes}') LOOP
        IF pg_catalog.jsonb_typeof(reason)<>'string'
           OR reason#>>'{}'!~'^[A-Za-z0-9][A-Za-z0-9._:-]*$' THEN
            RAISE EXCEPTION 'invalid dashboard Starlink reason code'
                USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF (SELECT pg_catalog.count(DISTINCT value->>'candidate_id')
          FROM pg_catalog.jsonb_array_elements(p_view->'candidates'))<>target_count THEN
        RAISE EXCEPTION 'duplicate dashboard Starlink candidate'
            USING ERRCODE='22023';
    END IF;
    PERFORM 1 FROM public.recording_starlink_candidate AS source
      JOIN public.dashboard_recording_detail_projection AS detail
        ON detail.recording_id=source.recording_id
     WHERE source.recording_id=target_recording
       AND source.analysis_id=target_analysis
       AND source.bundle_digest_algorithm='sha256'
       AND source.bundle_digest_value=target_digest
       AND source.candidate_count=target_count
       AND source.analysis_id=(SELECT work.analysis_id
            FROM public.starlink_projection_work AS work
            WHERE work.work_id=p_work_id)
       AND source.recording_id=(SELECT work.recording_id
            FROM public.starlink_projection_work AS work
            WHERE work.work_id=p_work_id)
       AND source.bundle_digest_value=(SELECT work.bundle_digest_value
            FROM public.starlink_projection_work AS work
            WHERE work.work_id=p_work_id);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'dashboard Starlink source is not authoritative'
            USING ERRCODE='23503';
    END IF;
    INSERT INTO public.dashboard_recording_starlink_projection(
        recording_id,analysis_id,bundle_digest_value,semantic_view)
    VALUES(target_recording,target_analysis,target_digest,p_view)
    ON CONFLICT DO NOTHING
    RETURNING projection_sequence INTO inserted_sequence;
    IF inserted_sequence IS NOT NULL THEN RETURN inserted_sequence; END IF;
    SELECT projection_sequence INTO existing_sequence
      FROM public.dashboard_recording_starlink_projection
     WHERE recording_id=target_recording AND analysis_id=target_analysis
       AND bundle_digest_value=target_digest AND semantic_view=p_view;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'dashboard Starlink projection identity conflict'
            USING ERRCODE='23505';
    END IF;
    RETURN existing_sequence;
END $function$;

CREATE FUNCTION public.read_starlink_analysis_receipt(p_source_job_id text)
RETURNS TABLE(work_id text,source_job_id text,work_state text,
    analysis_id text,recording_id text,input_digest_algorithm text,
    input_digest_value text,request_digest_algorithm text,request_digest_value text,
    bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,
    bundle_media_type text,bundle_format_id text,bundle_locator text,
    candidate_count integer,analyzed_stream_count integer,
    projected_at_utc timestamptz,job_state text,job_result_ref jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    SELECT q.work_id,q.source_job_id,q.state,s.analysis_id,s.recording_id,
           s.input_recording_digest_algorithm,s.input_recording_digest_value,
           s.request_digest_algorithm,s.request_digest_value,
           s.bundle_digest_algorithm,s.bundle_digest_value,
           o.byte_count,o.media_type,o.format_id,o.locator,
           s.candidate_count,s.analyzed_stream_count,q.projected_at_utc,
           j.state,j.result_ref
      FROM public.starlink_projection_work AS q
      JOIN public.recording_starlink_candidate AS s ON s.analysis_id=q.analysis_id
      JOIN public.object_blob AS o ON (o.digest_algorithm,o.digest_value)=
           (s.bundle_digest_algorithm,s.bundle_digest_value)
      JOIN public.job AS j ON j.job_id=q.source_job_id
     WHERE q.source_job_id=p_source_job_id AND j.job_type='starlink_analysis'
       AND o.lifecycle_state='live';
$function$;

CREATE OR REPLACE FUNCTION public.capture_analysis_inactive()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    SELECT NOT EXISTS (SELECT 1 FROM public.job AS j
        WHERE j.state='leased'
          AND j.job_type IN ('recording_analysis','model_analysis',
                             'waterfall_analysis','starlink_analysis')
          AND j.lease_expires_utc>pg_catalog.clock_timestamp())
       AND NOT EXISTS (SELECT 1 FROM public.feature_projection_work AS w
        WHERE w.state='leased' AND w.lease_expires_utc>pg_catalog.clock_timestamp())
       AND NOT EXISTS (SELECT 1 FROM public.waterfall_projection_work AS w
        WHERE w.state='leased' AND w.lease_expires_utc>pg_catalog.clock_timestamp())
       AND NOT EXISTS (SELECT 1 FROM public.starlink_projection_work AS w
        WHERE w.state='leased' AND w.lease_expires_utc>pg_catalog.clock_timestamp());
$function$;

CREATE OR REPLACE FUNCTION public.capture_analysis_drain_ready()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
    SELECT NOT EXISTS (SELECT 1 FROM public.job AS j
        WHERE j.job_type IN ('recording_analysis','waterfall_analysis',
                             'starlink_analysis')
          AND j.state IN ('ready','leased','failed'))
       AND NOT EXISTS (SELECT 1 FROM public.feature_projection_work AS w
        WHERE w.state IN ('ready','leased','failed'))
       AND NOT EXISTS (SELECT 1 FROM public.waterfall_projection_work AS w
        WHERE w.state IN ('ready','leased','failed'))
       AND NOT EXISTS (SELECT 1 FROM public.starlink_projection_work AS w
        WHERE w.state IN ('ready','leased','failed'));
$function$;

ALTER FUNCTION public.publish_recording_starlink_candidate(text,text,text,text,text,text,text,text,integer,integer,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_starlink_projection_work(text,text,text,bigint,text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_starlink_projection_work(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_starlink_projection_work(text,text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_starlink_projection_work(text,text,bigint,text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_starlink_projection_work(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_dashboard_recording_starlink(jsonb,text,text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_starlink_analysis_receipt(text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_analysis_inactive() OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_analysis_drain_ready() OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION
 public.publish_recording_starlink_candidate(text,text,text,text,text,text,text,text,integer,integer,text),
 public.publish_starlink_projection_work(text,text,text,bigint,text,text,text,text),
 public.claim_starlink_projection_work(text,interval),
 public.complete_starlink_projection_work(text,text,bigint),
 public.retry_starlink_projection_work(text,text,bigint,text,interval),
 public.park_starlink_projection_work(text,text,bigint,text),
 public.publish_dashboard_recording_starlink(jsonb,text,text,bigint),
 public.read_starlink_analysis_receipt(text),
 public.capture_analysis_inactive(),public.capture_analysis_drain_ready()
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;

GRANT EXECUTE ON FUNCTION
 public.publish_recording_starlink_candidate(text,text,text,text,text,text,text,text,integer,integer,text),
 public.publish_starlink_projection_work(text,text,text,bigint,text,text,text,text),
 public.claim_starlink_projection_work(text,interval),
 public.complete_starlink_projection_work(text,text,bigint),
 public.retry_starlink_projection_work(text,text,bigint,text,interval),
 public.park_starlink_projection_work(text,text,bigint,text),
 public.publish_dashboard_recording_starlink(jsonb,text,text,bigint),
 public.read_starlink_analysis_receipt(text)
TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.capture_analysis_inactive(),
 public.capture_analysis_drain_ready() TO leo_capture;

COMMIT;
