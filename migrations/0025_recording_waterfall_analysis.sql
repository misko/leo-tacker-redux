BEGIN;

CREATE TABLE public.recording_waterfall (
    product_id text PRIMARY KEY CHECK (product_id ~ '^waterfall_[0-9a-f]{32}$'),
    analysis_run_id text NOT NULL UNIQUE CHECK (analysis_run_id ~ '^arun_[0-9a-f]{32}$'),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    input_recording_digest_algorithm text NOT NULL
        CHECK (input_recording_digest_algorithm = 'sha256'),
    input_recording_digest_value text NOT NULL
        CHECK (input_recording_digest_value ~ '^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm = 'sha256'),
    request_digest_value text NOT NULL CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm = 'sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    tile_count integer NOT NULL CHECK (tile_count BETWEEN 1 AND 64),
    cell_count integer NOT NULL CHECK (cell_count BETWEEN 1 AND 262144),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recording_id, input_recording_digest_algorithm,
            input_recording_digest_value, request_digest_algorithm,
            request_digest_value),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm, digest_value)
);

CREATE TRIGGER recording_waterfall_bundle_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value
ON public.recording_waterfall
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
           'recording_waterfall.bundle', product_id::text
           FROM public.recording_waterfall;

CREATE TABLE public.waterfall_projection_work (
    work_id text PRIMARY KEY CHECK (work_id ~ '^wfwork_[0-9a-f]{64}$'),
    work_schema_id text NOT NULL
        CHECK (work_schema_id = 'org.leo-flow.waterfall-projection-work'),
    work_schema_version text NOT NULL CHECK (work_schema_version = '0.1'),
    source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    product_id text NOT NULL UNIQUE REFERENCES public.recording_waterfall(product_id),
    analysis_run_id text NOT NULL,
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
    CHECK (
        (state = 'leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL)
        OR (state <> 'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)
    ),
    CHECK (
        (state = 'parked' AND park_reason ~ '^[a-z0-9][a-z0-9._:-]{0,127}$'
         AND parked_at_utc IS NOT NULL)
        OR (state <> 'parked' AND park_reason IS NULL AND parked_at_utc IS NULL)
    )
);

CREATE INDEX waterfall_projection_work_claim_idx
ON public.waterfall_projection_work(available_at_utc, work_id)
WHERE state IN ('ready', 'failed', 'leased');

GRANT SELECT, INSERT ON public.recording_waterfall,
    public.waterfall_projection_work TO leo_routine_owner;
GRANT UPDATE ON public.waterfall_projection_work TO leo_routine_owner;
GRANT SELECT ON public.recording_waterfall TO leo_analysis;
REVOKE ALL ON public.waterfall_projection_work
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.recording_waterfall
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

CREATE FUNCTION public.publish_recording_waterfall(
    p_product_id text, p_analysis_run_id text, p_recording_id text,
    p_input_digest_algorithm text, p_input_digest_value text,
    p_request_digest_algorithm text, p_request_digest_value text,
    p_bundle_digest_algorithm text, p_bundle_digest_value text,
    p_tile_count integer, p_cell_count integer, p_idempotency_key text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH inserted AS (
        INSERT INTO public.recording_waterfall(
            product_id, analysis_run_id, recording_id,
            input_recording_digest_algorithm, input_recording_digest_value,
            request_digest_algorithm, request_digest_value,
            bundle_digest_algorithm, bundle_digest_value,
            tile_count, cell_count, idempotency_key)
        VALUES (p_product_id, p_analysis_run_id, p_recording_id,
            p_input_digest_algorithm, p_input_digest_value,
            p_request_digest_algorithm, p_request_digest_value,
            p_bundle_digest_algorithm, p_bundle_digest_value,
            p_tile_count, p_cell_count, p_idempotency_key)
        ON CONFLICT DO NOTHING RETURNING product_id)
    SELECT pg_catalog.count(*) = 1 FROM inserted;
$function$;

CREATE FUNCTION public.publish_waterfall_projection_work(
    p_work_id text, p_source_job_id text, p_product_id text,
    p_analysis_run_id text, p_recording_id text,
    p_bundle_digest_algorithm text, p_bundle_digest_value text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH candidate AS (
        SELECT p_work_id AS work_id, p_source_job_id AS source_job_id,
               p_product_id AS product_id, p_analysis_run_id AS analysis_run_id,
               p_recording_id AS recording_id,
               p_bundle_digest_algorithm AS bundle_digest_algorithm,
               p_bundle_digest_value AS bundle_digest_value
          FROM public.job AS j
          JOIN public.recording_waterfall AS w ON w.product_id = p_product_id
         WHERE j.job_id = p_source_job_id
           AND j.job_type = 'waterfall_analysis'
           AND w.analysis_run_id = p_analysis_run_id
           AND w.recording_id = p_recording_id
           AND (w.bundle_digest_algorithm, w.bundle_digest_value) =
               (p_bundle_digest_algorithm, p_bundle_digest_value)
    ), inserted AS (
        INSERT INTO public.waterfall_projection_work(
            work_id, work_schema_id, work_schema_version, source_job_id,
            product_id, analysis_run_id, recording_id,
            bundle_digest_algorithm, bundle_digest_value)
        SELECT work_id, 'org.leo-flow.waterfall-projection-work', '0.1',
               source_job_id, product_id, analysis_run_id, recording_id,
               bundle_digest_algorithm, bundle_digest_value FROM candidate
        ON CONFLICT DO NOTHING RETURNING work_id)
    SELECT EXISTS (SELECT 1 FROM inserted) OR (
      EXISTS (SELECT 1 FROM candidate) AND EXISTS (
        SELECT 1 FROM public.waterfall_projection_work AS existing
         WHERE existing.work_id = p_work_id
           AND existing.source_job_id = p_source_job_id
           AND existing.product_id = p_product_id
           AND existing.analysis_run_id = p_analysis_run_id
           AND existing.recording_id = p_recording_id
           AND existing.bundle_digest_algorithm = p_bundle_digest_algorithm
           AND existing.bundle_digest_value = p_bundle_digest_value
      )
    );
$function$;

CREATE FUNCTION public.claim_waterfall_projection_work(
    p_lease_token text, p_ttl_interval interval)
RETURNS SETOF public.waterfall_projection_work
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lease_token IS NULL OR p_lease_token = '' OR p_ttl_interval <= interval '0' THEN
        RAISE EXCEPTION 'invalid waterfall work claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY WITH candidate AS (
        SELECT w.work_id FROM public.waterfall_projection_work AS w
         WHERE w.available_at_utc <= pg_catalog.clock_timestamp()
           AND (w.state IN ('ready','failed') OR
                (w.state='leased' AND w.lease_expires_utc <= pg_catalog.clock_timestamp()))
         ORDER BY w.available_at_utc, w.work_id FOR UPDATE SKIP LOCKED LIMIT 1)
    UPDATE public.waterfall_projection_work AS w
       SET state='leased', attempt=w.attempt+1,
           lease_generation=w.lease_generation+1, lease_token=p_lease_token,
           lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl_interval
      FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*;
END $function$;

CREATE FUNCTION public.heartbeat_waterfall_projection_work(
    p_work_id text, p_lease_token text, p_generation bigint, p_ttl interval)
RETURNS SETOF public.waterfall_projection_work
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.waterfall_projection_work SET lease_expires_utc=pg_catalog.clock_timestamp()+p_ttl
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation AND lease_expires_utc>pg_catalog.clock_timestamp()
       AND p_ttl>interval '0' RETURNING *;
$function$;

CREATE FUNCTION public.complete_waterfall_projection_work(
    p_work_id text, p_lease_token text, p_generation bigint)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (UPDATE public.waterfall_projection_work
       SET state='succeeded', projected_at_utc=pg_catalog.clock_timestamp(),
           lease_token=NULL, lease_expires_utc=NULL, last_error=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation AND lease_expires_utc>pg_catalog.clock_timestamp()
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.retry_waterfall_projection_work(
    p_work_id text, p_lease_token text, p_generation bigint,
    p_reason text, p_delay interval)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (UPDATE public.waterfall_projection_work
       SET state='failed', last_error=p_reason,
           available_at_utc=pg_catalog.clock_timestamp()+p_delay,
           lease_token=NULL, lease_expires_utc=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation AND lease_expires_utc>pg_catalog.clock_timestamp()
       AND p_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$' AND p_delay>interval '0'
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.park_waterfall_projection_work(
    p_work_id text, p_lease_token text, p_generation bigint, p_reason text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (UPDATE public.waterfall_projection_work
       SET state='parked', park_reason=p_reason,
           parked_at_utc=pg_catalog.clock_timestamp(),
           lease_token=NULL, lease_expires_utc=NULL, last_error=NULL
     WHERE work_id=p_work_id AND state='leased' AND lease_token=p_lease_token
       AND lease_generation=p_generation AND lease_expires_utc>pg_catalog.clock_timestamp()
       AND p_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$'
     RETURNING work_id) SELECT pg_catalog.count(*)=1 FROM changed;
$function$;

CREATE FUNCTION public.read_waterfall_analysis_receipt(p_source_job_id text)
RETURNS TABLE(
    work_id text, source_job_id text, work_state text, product_id text,
    analysis_run_id text, recording_id text,
    input_digest_algorithm text, input_digest_value text,
    request_digest_algorithm text, request_digest_value text,
    bundle_digest_algorithm text, bundle_digest_value text,
    bundle_byte_count bigint, bundle_media_type text,
    bundle_format_id text, bundle_locator text,
    tile_count integer, cell_count integer, projected_at_utc timestamptz,
    job_state text, job_result_ref jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT q.work_id, q.source_job_id, q.state, w.product_id,
           w.analysis_run_id, w.recording_id,
           w.input_recording_digest_algorithm, w.input_recording_digest_value,
           w.request_digest_algorithm, w.request_digest_value,
           w.bundle_digest_algorithm, w.bundle_digest_value,
           o.byte_count, o.media_type, o.format_id, o.locator,
           w.tile_count, w.cell_count, q.projected_at_utc, j.state, j.result_ref
      FROM public.waterfall_projection_work AS q
      JOIN public.recording_waterfall AS w ON w.product_id=q.product_id
      JOIN public.object_blob AS o ON (o.digest_algorithm,o.digest_value)=
           (w.bundle_digest_algorithm,w.bundle_digest_value)
      JOIN public.job AS j ON j.job_id=q.source_job_id
     WHERE q.source_job_id=p_source_job_id AND j.job_type='waterfall_analysis'
       AND o.lifecycle_state='live';
$function$;

-- Migration 0024 gates capture against the queues that existed at that head.
-- Extend the same narrow observation to this new analysis and projection lane.
-- Pending or expired work remains safe deferred backlog; only current leases
-- prove that analysis owns the shared capture/analysis mode.
CREATE OR REPLACE FUNCTION public.capture_analysis_inactive()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
               SELECT 1
                 FROM public.job AS active_job
                WHERE active_job.state = 'leased'
                  AND active_job.job_type IN (
                      'recording_analysis', 'model_analysis',
                      'waterfall_analysis'
                  )
                  AND active_job.lease_expires_utc
                      > pg_catalog.clock_timestamp()
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.feature_projection_work AS active_projection
                WHERE active_projection.state = 'leased'
                  AND active_projection.lease_expires_utc
                      > pg_catalog.clock_timestamp()
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.waterfall_projection_work AS active_waterfall
                WHERE active_waterfall.state = 'leased'
                  AND active_waterfall.lease_expires_utc
                      > pg_catalog.clock_timestamp()
           );
$function$;

ALTER FUNCTION public.publish_recording_waterfall(text,text,text,text,text,text,text,text,text,integer,integer,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_waterfall_projection_work(text,text,text,text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_waterfall_projection_work(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.heartbeat_waterfall_projection_work(text,text,bigint,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_waterfall_projection_work(text,text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_waterfall_projection_work(text,text,bigint,text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_waterfall_projection_work(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_waterfall_analysis_receipt(text) OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION
    public.publish_recording_waterfall(text,text,text,text,text,text,text,text,text,integer,integer,text),
    public.publish_waterfall_projection_work(text,text,text,text,text,text,text),
    public.claim_waterfall_projection_work(text,interval),
    public.heartbeat_waterfall_projection_work(text,text,bigint,interval),
    public.complete_waterfall_projection_work(text,text,bigint),
    public.retry_waterfall_projection_work(text,text,bigint,text,interval),
    public.park_waterfall_projection_work(text,text,bigint,text),
    public.read_waterfall_analysis_receipt(text)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT EXECUTE ON FUNCTION
    public.publish_recording_waterfall(text,text,text,text,text,text,text,text,text,integer,integer,text),
    public.publish_waterfall_projection_work(text,text,text,text,text,text,text),
    public.claim_waterfall_projection_work(text,interval),
    public.heartbeat_waterfall_projection_work(text,text,bigint,interval),
    public.complete_waterfall_projection_work(text,text,bigint),
    public.retry_waterfall_projection_work(text,text,bigint,text,interval),
    public.park_waterfall_projection_work(text,text,bigint,text)
TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_waterfall_analysis_receipt(text)
TO leo_analysis, leo_capture;

COMMIT;
