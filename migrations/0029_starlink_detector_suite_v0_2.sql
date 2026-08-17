BEGIN;

CREATE TABLE public.recording_starlink_detector_suite (
    analysis_id text PRIMARY KEY CHECK (analysis_id ~ '^slsuite_[0-9a-f]{32}$'),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    input_recording_digest_algorithm text NOT NULL CHECK (input_recording_digest_algorithm='sha256'),
    input_recording_digest_value text NOT NULL CHECK (input_recording_digest_value~'^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm='sha256'),
    request_digest_value text NOT NULL CHECK (request_digest_value~'^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm='sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value~'^[0-9a-f]{64}$'),
    result_state text NOT NULL CHECK (result_state IN ('candidates','not_evaluated')),
    suite_count integer NOT NULL CHECK (suite_count BETWEEN 0 AND 64),
    method_count integer NOT NULL CHECK (method_count BETWEEN 0 AND 512),
    CHECK ((result_state='not_evaluated' AND suite_count=0 AND method_count=0)
        OR (result_state='candidates' AND suite_count>0 AND method_count=suite_count*8)),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key<>''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(recording_id,input_recording_digest_algorithm,input_recording_digest_value,request_digest_algorithm,request_digest_value),
    FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE TRIGGER recording_starlink_detector_suite_bundle_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value
ON public.recording_starlink_detector_suite FOR EACH ROW
EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE OR REPLACE VIEW public.object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,data_digest_value AS digest_value,'recording.data'::text AS reference_kind,recording_id::text AS owner_id FROM public.recording
UNION ALL SELECT metadata_digest_algorithm,metadata_digest_value,'recording.metadata',recording_id::text FROM public.recording
UNION ALL SELECT raw_digest_algorithm,raw_digest_value,'ephemeris_snapshot.raw',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT normalized_digest_algorithm,normalized_digest_value,'ephemeris_snapshot.normalized',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT provenance_digest_algorithm,provenance_digest_value,'ephemeris_snapshot.provenance',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'dataset_snapshot.bundle',snapshot_id::text FROM public.dataset_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'feature_set.bundle',feature_set_id::text FROM public.feature_set
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'model_snapshot.bundle',model_snapshot_id::text FROM public.model_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'hardware_snapshot.bundle',snapshot_id::text FROM public.hardware_snapshot
UNION ALL SELECT report_digest_algorithm,report_digest_value,'detector_evaluation_report.report',evaluation_id::text FROM public.detector_evaluation_report
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'tracking_input_snapshot.bundle',snapshot_id::text FROM public.tracking_input_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'tracking_model_snapshot.bundle',model_run_id::text FROM public.tracking_model_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_waterfall.bundle',product_id::text FROM public.recording_waterfall
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_candidate.bundle',analysis_id::text FROM public.recording_starlink_candidate
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_detector_suite.bundle',analysis_id::text FROM public.recording_starlink_detector_suite;

CREATE TABLE public.starlink_detector_suite_projection_work (
    work_id text PRIMARY KEY CHECK(work_id~'^slsuitework_[0-9a-f]{64}$'),
    source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    analysis_id text NOT NULL UNIQUE REFERENCES public.recording_starlink_detector_suite(analysis_id),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    bundle_digest_algorithm text NOT NULL CHECK(bundle_digest_algorithm='sha256'),
    bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'ready' CHECK(state IN ('ready','leased','failed','succeeded','parked')),
    available_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0),
    lease_token text,lease_generation bigint NOT NULL DEFAULT 0 CHECK(lease_generation>=0),lease_expires_utc timestamptz,last_error text,park_reason text,parked_at_utc timestamptz,projected_at_utc timestamptz,
    CHECK((state='leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL) OR (state<>'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)),
    CHECK((state='parked' AND park_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$' AND parked_at_utc IS NOT NULL) OR (state<>'parked' AND park_reason IS NULL AND parked_at_utc IS NULL)),
    CHECK((state='succeeded' AND projected_at_utc IS NOT NULL) OR (state<>'succeeded' AND projected_at_utc IS NULL))
);
CREATE INDEX starlink_detector_suite_projection_claim_idx ON public.starlink_detector_suite_projection_work(available_at_utc,work_id) WHERE state IN ('ready','failed','leased');

CREATE TABLE public.dashboard_recording_starlink_detector_suite_projection (
    projection_sequence bigint PRIMARY KEY DEFAULT nextval('public.dashboard_projection_sequence'),
    recording_id text NOT NULL CHECK(recording_id~'^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    analysis_id text NOT NULL CHECK(analysis_id~'^slsuite_[0-9a-f]{32}$'),
    bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
    semantic_view jsonb NOT NULL CHECK(jsonb_typeof(semantic_view)='object' AND octet_length(semantic_view::text)<=4194304),
    projected_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(recording_id,analysis_id,bundle_digest_value)
);
CREATE INDEX dashboard_recording_starlink_detector_suite_latest_idx ON public.dashboard_recording_starlink_detector_suite_projection(recording_id,projection_sequence DESC);

CREATE FUNCTION public.publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
WITH inserted AS (INSERT INTO public.recording_starlink_detector_suite(analysis_id,recording_id,input_recording_digest_algorithm,input_recording_digest_value,request_digest_algorithm,request_digest_value,bundle_digest_algorithm,bundle_digest_value,result_state,suite_count,method_count,idempotency_key)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT DO NOTHING RETURNING analysis_id) SELECT count(*)=1 FROM inserted;
$f$;

CREATE FUNCTION public.publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
DECLARE inserted text; BEGIN
 IF $1!~'^slsuitework_[0-9a-f]{64}$' OR $3='' OR $4<=0 THEN RAISE EXCEPTION 'invalid detector-suite projection publication' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM public.job j WHERE j.job_id=$2 AND j.job_type='starlink_suite_analysis' AND j.state='leased' AND j.lease_token=$3 AND j.lease_generation=$4 AND j.lease_expires_utc>clock_timestamp() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'detector-suite source lease is not current' USING ERRCODE='55000'; END IF;
 PERFORM 1 FROM public.recording_starlink_detector_suite s JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value) WHERE s.analysis_id=$5 AND s.recording_id=$6 AND (s.bundle_digest_algorithm,s.bundle_digest_value)=($7,$8) AND o.lifecycle_state='live';
 IF NOT FOUND THEN RAISE EXCEPTION 'detector-suite source is not authoritative' USING ERRCODE='23503'; END IF;
 INSERT INTO public.starlink_detector_suite_projection_work(work_id,source_job_id,analysis_id,recording_id,bundle_digest_algorithm,bundle_digest_value) VALUES($1,$2,$5,$6,$7,$8) ON CONFLICT DO NOTHING RETURNING work_id INTO inserted;
 IF inserted IS NULL AND NOT EXISTS(SELECT 1 FROM public.starlink_detector_suite_projection_work WHERE work_id=$1 AND source_job_id=$2 AND analysis_id=$5 AND recording_id=$6 AND bundle_digest_algorithm=$7 AND bundle_digest_value=$8) THEN RAISE EXCEPTION 'detector-suite projection identity conflict' USING ERRCODE='23505'; END IF;
 RETURN true; END $f$;

CREATE FUNCTION public.claim_starlink_detector_suite_projection_work(text,interval) RETURNS SETOF public.starlink_detector_suite_projection_work LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
BEGIN IF $1='' OR $2<=interval '0' THEN RAISE EXCEPTION 'invalid detector-suite projection claim' USING ERRCODE='22023'; END IF;
RETURN QUERY WITH candidate AS (SELECT work_id FROM public.starlink_detector_suite_projection_work WHERE available_at_utc<=clock_timestamp() AND (state IN('ready','failed') OR (state='leased' AND lease_expires_utc<=clock_timestamp())) ORDER BY available_at_utc,work_id FOR UPDATE SKIP LOCKED LIMIT 1)
UPDATE public.starlink_detector_suite_projection_work w SET state='leased',attempt=w.attempt+1,lease_generation=w.lease_generation+1,lease_token=$1,lease_expires_utc=clock_timestamp()+$2,last_error=NULL FROM candidate WHERE w.work_id=candidate.work_id RETURNING w.*; END $f$;

CREATE FUNCTION public.complete_starlink_detector_suite_projection_work(text,text,bigint) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$ WITH changed AS (UPDATE public.starlink_detector_suite_projection_work SET state='succeeded',projected_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL WHERE work_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() RETURNING work_id) SELECT count(*)=1 FROM changed;$f$;
CREATE FUNCTION public.retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$ WITH changed AS (UPDATE public.starlink_detector_suite_projection_work SET state='failed',last_error=$4,available_at_utc=clock_timestamp()+$5,lease_token=NULL,lease_expires_utc=NULL WHERE work_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' AND $5>interval '0' RETURNING work_id) SELECT count(*)=1 FROM changed;$f$;
CREATE FUNCTION public.park_starlink_detector_suite_projection_work(text,text,bigint,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$ WITH changed AS (UPDATE public.starlink_detector_suite_projection_work SET state='parked',park_reason=$4,parked_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL WHERE work_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' RETURNING work_id) SELECT count(*)=1 FROM changed;$f$;

CREATE FUNCTION public.publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
DECLARE rec text; aid text; dig text; seq bigint; BEGIN
 PERFORM 1 FROM public.starlink_detector_suite_projection_work WHERE work_id=$2 AND state='leased' AND lease_token=$3 AND lease_generation=$4 AND lease_expires_utc>clock_timestamp() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'detector-suite projection lease is not current' USING ERRCODE='55000'; END IF;
 IF $1#>>'{schema,schema_id}'<>'org.leo-flow.dashboard.recording-starlink-detector-suite'
    OR $1#>>'{schema,version,major}'<>'0' OR $1#>>'{schema,version,minor}'<>'2'
    OR NOT ($1?'calibrated_detection_count')
    OR jsonb_typeof($1->'calibrated_detection_count')<>'null'
    OR $1->>'state' NOT IN('candidates','not_evaluated')
    OR jsonb_typeof($1->'methods')<>'array'
    OR jsonb_array_length($1->'methods')<>(($1->>'method_count')::integer)
 THEN RAISE EXCEPTION 'invalid detector-suite dashboard projection' USING ERRCODE='22023'; END IF;
 rec:=$1->>'recording_id'; aid:=$1#>>'{analysis_ref,artifact_id}'; dig:=$1#>>'{analysis_ref,digest,value}';
 PERFORM 1 FROM public.starlink_detector_suite_projection_work w JOIN public.recording_starlink_detector_suite s ON s.analysis_id=w.analysis_id WHERE w.work_id=$2 AND s.recording_id=rec AND s.analysis_id=aid AND s.bundle_digest_value=dig AND s.result_state=$1->>'state' AND s.suite_count=($1->>'analyzed_stream_count')::integer AND s.method_count=($1->>'method_count')::integer;
 IF NOT FOUND THEN RAISE EXCEPTION 'detector-suite dashboard source differs' USING ERRCODE='23503'; END IF;
 INSERT INTO public.dashboard_recording_starlink_detector_suite_projection(recording_id,analysis_id,bundle_digest_value,semantic_view) VALUES(rec,aid,dig,$1) ON CONFLICT DO NOTHING RETURNING projection_sequence INTO seq;
 IF seq IS NULL THEN SELECT projection_sequence INTO seq FROM public.dashboard_recording_starlink_detector_suite_projection WHERE recording_id=rec AND analysis_id=aid AND bundle_digest_value=dig AND semantic_view=$1; END IF;
 IF seq IS NULL THEN RAISE EXCEPTION 'detector-suite dashboard identity conflict' USING ERRCODE='23505'; END IF; RETURN seq; END $f$;

CREATE FUNCTION public.read_starlink_detector_suite_receipt(text) RETURNS TABLE(work_id text,source_job_id text,work_state text,analysis_id text,recording_id text,result_state text,suite_count integer,method_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text,projected_at_utc timestamptz,job_state text,job_result_ref jsonb) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
SELECT w.work_id,w.source_job_id,w.state,s.analysis_id,s.recording_id,s.result_state,s.suite_count,s.method_count,s.bundle_digest_algorithm,s.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator,w.projected_at_utc,j.state,j.result_ref FROM public.starlink_detector_suite_projection_work w JOIN public.recording_starlink_detector_suite s ON s.analysis_id=w.analysis_id JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value) JOIN public.job j ON j.job_id=w.source_job_id WHERE w.source_job_id=$1 AND j.job_type='starlink_suite_analysis' AND o.lifecycle_state='live';$f$;

CREATE OR REPLACE FUNCTION public.capture_analysis_inactive() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
SELECT NOT EXISTS(SELECT 1 FROM public.job WHERE state='leased' AND job_type IN('recording_analysis','model_analysis','waterfall_analysis','starlink_analysis','starlink_suite_analysis') AND lease_expires_utc>clock_timestamp()) AND NOT EXISTS(SELECT 1 FROM public.feature_projection_work WHERE state='leased' AND lease_expires_utc>clock_timestamp()) AND NOT EXISTS(SELECT 1 FROM public.waterfall_projection_work WHERE state='leased' AND lease_expires_utc>clock_timestamp()) AND NOT EXISTS(SELECT 1 FROM public.starlink_projection_work WHERE state='leased' AND lease_expires_utc>clock_timestamp()) AND NOT EXISTS(SELECT 1 FROM public.starlink_detector_suite_projection_work WHERE state='leased' AND lease_expires_utc>clock_timestamp());$f$;
CREATE OR REPLACE FUNCTION public.capture_analysis_drain_ready() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
SELECT NOT EXISTS(SELECT 1 FROM public.job WHERE job_type IN('recording_analysis','waterfall_analysis','starlink_analysis','starlink_suite_analysis') AND state IN('ready','leased','failed')) AND NOT EXISTS(SELECT 1 FROM public.feature_projection_work WHERE state IN('ready','leased','failed')) AND NOT EXISTS(SELECT 1 FROM public.waterfall_projection_work WHERE state IN('ready','leased','failed')) AND NOT EXISTS(SELECT 1 FROM public.starlink_projection_work WHERE state IN('ready','leased','failed')) AND NOT EXISTS(SELECT 1 FROM public.starlink_detector_suite_projection_work WHERE state IN('ready','leased','failed'));$f$;

GRANT SELECT,INSERT ON public.recording_starlink_detector_suite,public.starlink_detector_suite_projection_work,public.dashboard_recording_starlink_detector_suite_projection TO leo_routine_owner;
GRANT UPDATE ON public.starlink_detector_suite_projection_work TO leo_routine_owner;
GRANT SELECT ON public.recording_starlink_detector_suite TO leo_analysis;
GRANT SELECT ON public.dashboard_recording_starlink_detector_suite_projection TO leo_dashboard;
REVOKE ALL ON public.starlink_detector_suite_projection_work FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON public.recording_starlink_detector_suite,public.dashboard_recording_starlink_detector_suite_projection FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;

ALTER FUNCTION public.publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_starlink_detector_suite_projection_work(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_starlink_detector_suite_projection_work(text,text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_starlink_detector_suite_projection_work(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_starlink_detector_suite_receipt(text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_analysis_inactive() OWNER TO leo_routine_owner;
ALTER FUNCTION public.capture_analysis_drain_ready() OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text),public.publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text),public.claim_starlink_detector_suite_projection_work(text,interval),public.complete_starlink_detector_suite_projection_work(text,text,bigint),public.retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval),public.park_starlink_detector_suite_projection_work(text,text,bigint,text),public.publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint),public.read_starlink_detector_suite_receipt(text),public.capture_analysis_inactive(),public.capture_analysis_drain_ready() FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text),public.publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text),public.claim_starlink_detector_suite_projection_work(text,interval),public.complete_starlink_detector_suite_projection_work(text,text,bigint),public.retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval),public.park_starlink_detector_suite_projection_work(text,text,bigint,text),public.publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint),public.read_starlink_detector_suite_receipt(text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.capture_analysis_inactive(),public.capture_analysis_drain_ready() TO leo_capture;

COMMIT;
