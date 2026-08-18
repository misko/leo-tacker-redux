BEGIN;

CREATE TABLE public.recording_full_dwell_timeline_v0_1 (
  analysis_id text PRIMARY KEY CHECK(analysis_id~'^fdtl_[0-9a-f]{32}$'),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  recording_identity_digest_value text NOT NULL CHECK(recording_identity_digest_value~'^[0-9a-f]{64}$'),
  request_digest_value text NOT NULL CHECK(request_digest_value~'^[0-9a-f]{64}$'),
  data_digest_value text NOT NULL CHECK(data_digest_value~'^[0-9a-f]{64}$'),
  metadata_digest_value text NOT NULL CHECK(metadata_digest_value~'^[0-9a-f]{64}$'),
  manifest_digest_value text NOT NULL CHECK(manifest_digest_value~'^[0-9a-f]{64}$'),
  bundle_digest_algorithm text NOT NULL CHECK(bundle_digest_algorithm='sha256'),
  bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
  stream_count integer NOT NULL CHECK(stream_count BETWEEN 1 AND 16),
  window_count integer NOT NULL CHECK(window_count BETWEEN stream_count AND stream_count*16384),
  covered_sample_count bigint NOT NULL CHECK(covered_sample_count>=window_count),
  idempotency_key text NOT NULL UNIQUE CHECK(idempotency_key<>''),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  UNIQUE(recording_id,recording_identity_digest_value,request_digest_value),
  FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);
CREATE INDEX recording_full_dwell_timeline_latest_v0_1_idx ON public.recording_full_dwell_timeline_v0_1(recording_id,published_at_utc DESC,analysis_id DESC);
CREATE TRIGGER recording_full_dwell_timeline_bundle_live BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value ON public.recording_full_dwell_timeline_v0_1 FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE TABLE public.full_dwell_timeline_work_v0_1 (
  work_id text PRIMARY KEY CHECK(work_id~'^fdtlw_[0-9a-f]{32}$'),
  recording_id text NOT NULL UNIQUE REFERENCES public.recording(recording_id),
  request_json jsonb NOT NULL CHECK(jsonb_typeof(request_json)='object'),
  state text NOT NULL DEFAULT 'ready' CHECK(state IN('ready','leased','failed','succeeded','parked')),
  available_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 8),
  lease_token text,
  lease_generation bigint NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_expires_utc timestamptz,
  last_error text,
  result_analysis_id text REFERENCES public.recording_full_dwell_timeline_v0_1(analysis_id),
  result_bundle_digest_value text,
  completed_at_utc timestamptz,
  CHECK((state='leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL) OR (state<>'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)),
  CHECK((state='succeeded' AND result_analysis_id IS NOT NULL AND result_bundle_digest_value IS NOT NULL AND result_bundle_digest_value~'^[0-9a-f]{64}$' AND completed_at_utc IS NOT NULL) OR (state<>'succeeded' AND result_analysis_id IS NULL AND result_bundle_digest_value IS NULL AND completed_at_utc IS NULL))
);
CREATE INDEX full_dwell_timeline_work_claim_v0_1_idx ON public.full_dwell_timeline_work_v0_1(available_at_utc DESC,work_id DESC) WHERE state IN('ready','failed','leased');

CREATE TABLE public.full_dwell_refinement_work_v0_1 (
  timeline_analysis_id text PRIMARY KEY REFERENCES public.recording_full_dwell_timeline_v0_1(analysis_id),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  request_json jsonb NOT NULL CHECK(jsonb_typeof(request_json)='object'),
  state text NOT NULL DEFAULT 'ready' CHECK(state IN('ready','failed','succeeded','parked')),
  admitted_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  last_error text
);

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
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_detector_suite.bundle',analysis_id::text FROM public.recording_starlink_detector_suite
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_waterfall_v0_2.bundle',product_id::text FROM public.recording_waterfall_v0_2
UNION ALL SELECT basic_bundle_digest_algorithm,basic_bundle_digest_value,'recording_doppler_analysis.basic',doppler_id::text FROM public.recording_doppler_analysis
UNION ALL SELECT advanced_bundle_digest_algorithm,advanced_bundle_digest_value,'recording_doppler_analysis.advanced',doppler_id::text FROM public.recording_doppler_analysis
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_surrogate_null.bundle',analysis_id::text FROM public.recording_starlink_surrogate_null
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_constellation.bundle',analysis_id::text FROM public.recording_starlink_pilot_constellation
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_temporal_pilot.bundle',analysis_id::text FROM public.recording_starlink_temporal_pilot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_full_dwell_v0_1.bundle',analysis_id::text FROM public.recording_starlink_full_dwell_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_acquired_constellation_v0_3.bundle',analysis_id::text FROM public.recording_starlink_acquired_constellation_v0_3
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_full_dwell_timeline_v0_1.bundle',analysis_id::text FROM public.recording_full_dwell_timeline_v0_1;

CREATE FUNCTION public.publish_recording_full_dwell_timeline_v0_1(jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE p alias for $1; inserted text;
BEGIN
 IF jsonb_typeof(p)<>'object' THEN RAISE EXCEPTION 'invalid timeline publication' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM public.recording r JOIN public.object_blob d ON (d.digest_algorithm,d.digest_value)=(r.data_digest_algorithm,r.data_digest_value) JOIN public.object_blob m ON (m.digest_algorithm,m.digest_value)=(r.metadata_digest_algorithm,r.metadata_digest_value)
 WHERE r.recording_id=p->>'recording_id' AND r.data_digest_value=p->>'data_digest_value' AND r.metadata_digest_value=p->>'metadata_digest_value' AND r.manifest_digest_value=p->>'manifest_digest_value' AND d.lifecycle_state='live' AND m.lifecycle_state='live';
 IF NOT FOUND THEN RAISE EXCEPTION 'timeline source recording is not exact and live' USING ERRCODE='23503'; END IF;
 INSERT INTO public.recording_full_dwell_timeline_v0_1 VALUES(p->>'analysis_id',p->>'recording_id',p->>'recording_identity_digest_value',p->>'request_digest_value',p->>'data_digest_value',p->>'metadata_digest_value',p->>'manifest_digest_value','sha256',p->>'bundle_digest_value',(p->>'stream_count')::integer,(p->>'window_count')::integer,(p->>'covered_sample_count')::bigint,p->>'idempotency_key',DEFAULT) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
 IF inserted IS NULL THEN
   IF EXISTS(SELECT 1 FROM public.recording_full_dwell_timeline_v0_1 e WHERE to_jsonb(e)-'published_at_utc'=p) THEN RETURN true; END IF;
   RAISE EXCEPTION 'timeline catalog identity conflict' USING ERRCODE='23505';
 END IF;
 RETURN true;
END $function$;

CREATE FUNCTION public.read_exact_recording_full_dwell_timeline_v0_1(text,text,text,text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,request_digest_value text,stream_count integer,window_count integer,covered_sample_count bigint,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.request_digest_value,h.stream_count,h.window_count,h.covered_sample_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_full_dwell_timeline_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.analysis_id=$1 AND h.recording_id=$2 AND h.bundle_digest_algorithm=$3 AND h.bundle_digest_value=$4 AND o.lifecycle_state='live';$function$;

CREATE FUNCTION public.read_latest_recording_full_dwell_timeline_v0_1(text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,request_digest_value text,stream_count integer,window_count integer,covered_sample_count bigint,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.request_digest_value,h.stream_count,h.window_count,h.covered_sample_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_full_dwell_timeline_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.recording_id=$1 AND o.lifecycle_state='live' ORDER BY h.published_at_utc DESC,h.analysis_id DESC LIMIT 1;$function$;

CREATE FUNCTION public.admit_full_dwell_timeline_work_v0_1(jsonb,jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE r alias for $1; q alias for $2; inserted text;
BEGIN
 IF jsonb_typeof(r)<>'object' OR jsonb_typeof(q)<>'object' OR jsonb_typeof(q->'streams')<>'array' OR jsonb_array_length(q->'streams') NOT BETWEEN 1 AND 16 OR (q->>'work_id')!~'^fdtlw_[0-9a-f]{32}$' OR q->>'recording_id'<>r->>'recording_id' THEN RAISE EXCEPTION 'invalid timeline admission' USING ERRCODE='22023'; END IF;
 IF q->'recording_ref'<>r THEN RAISE EXCEPTION 'timeline request recording differs' USING ERRCODE='23503'; END IF;
 PERFORM 1 FROM public.recording x JOIN public.object_blob d ON (d.digest_algorithm,d.digest_value)=(x.data_digest_algorithm,x.data_digest_value) JOIN public.object_blob m ON (m.digest_algorithm,m.digest_value)=(x.metadata_digest_algorithm,x.metadata_digest_value) JOIN public.recording_hardware_link l ON l.recording_id=x.recording_id WHERE x.recording_id=r->>'recording_id' AND x.data_digest_value=r->'data'->>'digest' AND x.metadata_digest_value=r->'metadata'->>'digest' AND x.manifest_digest_value=r->>'manifest_digest' AND d.byte_count=(r->'data'->>'byte_count')::bigint AND d.media_type=r->'data'->>'media_type' AND d.format_id=r->'data'->>'format_id' AND d.locator=r->'data'->>'locator' AND m.byte_count=(r->'metadata'->>'byte_count')::bigint AND m.media_type=r->'metadata'->>'media_type' AND m.format_id=r->'metadata'->>'format_id' AND m.locator=r->'metadata'->>'locator' AND d.lifecycle_state='live' AND m.lifecycle_state='live';
 IF NOT FOUND THEN RAISE EXCEPTION 'timeline admission source is not exact and live' USING ERRCODE='23503'; END IF;
 IF (q->>'capture_started_utc_ns')!~'^[0-9]{1,19}$' OR EXISTS(SELECT 1 FROM jsonb_array_elements(q->'streams') s WHERE NOT EXISTS(SELECT 1 FROM public.recording_hardware_link l JOIN public.hardware_receiver_chain c ON c.snapshot_id=l.hardware_snapshot_id WHERE l.recording_id=r->>'recording_id' AND c.receiver_chain_id=s->>'receiver_chain_id' AND c.radio_id=s->>'radio_id' AND c.lnb_id=s->>'lnb_id' AND c.valid_from_utc_ns<=(q->>'capture_started_utc_ns')::bigint AND (c.valid_until_utc_ns IS NULL OR (q->>'capture_started_utc_ns')::bigint<c.valid_until_utc_ns))) THEN RAISE EXCEPTION 'timeline stream hardware mapping is not authoritative' USING ERRCODE='23503'; END IF;
 IF EXISTS(SELECT 1 FROM public.full_dwell_timeline_work_v0_1 w WHERE w.work_id=q->>'work_id' AND w.recording_id=r->>'recording_id' AND w.request_json=q) THEN RETURN true; END IF;
 IF EXISTS(SELECT 1 FROM public.full_dwell_timeline_work_v0_1 w WHERE w.work_id=q->>'work_id' OR w.recording_id=r->>'recording_id') THEN RAISE EXCEPTION 'timeline work identity conflict' USING ERRCODE='23505'; END IF;
 PERFORM pg_catalog.pg_advisory_xact_lock(1186462802);
 IF (SELECT count(*) FROM public.full_dwell_timeline_work_v0_1 WHERE state IN('ready','leased','failed'))>=8 THEN RETURN false; END IF;
 INSERT INTO public.full_dwell_timeline_work_v0_1(work_id,recording_id,request_json) VALUES(q->>'work_id',r->>'recording_id',q) ON CONFLICT DO NOTHING RETURNING work_id INTO inserted;
 IF inserted IS NOT NULL THEN RETURN true; END IF;
 RAISE EXCEPTION 'timeline work identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.claim_full_dwell_timeline_work_v0_1(text,interval) RETURNS TABLE(work_id text,request_json jsonb,lease_token text,lease_generation bigint,attempt integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 IF $1='' OR $2<interval '1 second' OR $2>interval '8 hours' THEN RAISE EXCEPTION 'invalid timeline claim bounds' USING ERRCODE='22023'; END IF;
 RETURN QUERY WITH candidate AS (SELECT w.work_id FROM public.full_dwell_timeline_work_v0_1 w WHERE w.attempt<8 AND w.available_at_utc<=clock_timestamp() AND (w.state IN('ready','failed') OR (w.state='leased' AND w.lease_expires_utc<=clock_timestamp())) ORDER BY w.available_at_utc DESC,w.work_id DESC FOR UPDATE SKIP LOCKED LIMIT 1), claimed AS (UPDATE public.full_dwell_timeline_work_v0_1 w SET state='leased',attempt=w.attempt+1,lease_generation=w.lease_generation+1,lease_token=$1,lease_expires_utc=clock_timestamp()+$2,last_error=NULL FROM candidate c WHERE w.work_id=c.work_id RETURNING w.*) SELECT c.work_id,c.request_json,c.lease_token,c.lease_generation,c.attempt FROM claimed c;
END $function$;

CREATE FUNCTION public.list_full_dwell_timeline_candidates_v0_1(integer) RETURNS TABLE(recording_id text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE capacity integer;
BEGIN
 IF $1 NOT BETWEEN 1 AND 64 THEN RAISE EXCEPTION 'invalid timeline candidate bound' USING ERRCODE='22023'; END IF;
 SELECT greatest(0,8-count(*))::integer INTO capacity FROM public.full_dwell_timeline_work_v0_1 WHERE state IN('ready','leased','failed');
 RETURN QUERY SELECT r.recording_id FROM public.recording r JOIN public.object_blob d ON (d.digest_algorithm,d.digest_value)=(r.data_digest_algorithm,r.data_digest_value) JOIN public.object_blob m ON (m.digest_algorithm,m.digest_value)=(r.metadata_digest_algorithm,r.metadata_digest_value) JOIN public.recording_hardware_link l ON l.recording_id=r.recording_id WHERE d.lifecycle_state='live' AND m.lifecycle_state='live' AND NOT EXISTS(SELECT 1 FROM public.recording_full_dwell_timeline_v0_1 p WHERE p.recording_id=r.recording_id) AND NOT EXISTS(SELECT 1 FROM public.full_dwell_timeline_work_v0_1 w WHERE w.recording_id=r.recording_id) ORDER BY r.published_at DESC,r.recording_id DESC LIMIT least($1,capacity);
END $function$;

CREATE FUNCTION public.read_full_dwell_timeline_hardware_v0_1(text,bigint) RETURNS TABLE(receiver_chain_id text,radio_id text,lnb_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT c.receiver_chain_id,c.radio_id,c.lnb_id FROM public.recording_hardware_link l JOIN public.hardware_receiver_chain c ON c.snapshot_id=l.hardware_snapshot_id WHERE l.recording_id=$1 AND c.valid_from_utc_ns<=$2 AND (c.valid_until_utc_ns IS NULL OR $2<c.valid_until_utc_ns) ORDER BY c.receiver_chain_id;$function$;

CREATE FUNCTION public.complete_full_dwell_timeline_work_v0_1(text,text,bigint,text,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.full_dwell_timeline_work_v0_1 w SET state='succeeded',result_analysis_id=$4,result_bundle_digest_value=$5,completed_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL WHERE w.work_id=$1 AND w.state='leased' AND w.lease_token=$2 AND w.lease_generation=$3 AND w.lease_expires_utc>clock_timestamp() AND EXISTS(SELECT 1 FROM public.recording_full_dwell_timeline_v0_1 p WHERE p.analysis_id=$4 AND p.recording_id=w.recording_id AND p.bundle_digest_value=$5) RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.retry_full_dwell_timeline_work_v0_1(text,text,bigint,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.full_dwell_timeline_work_v0_1 SET state=CASE WHEN attempt>=8 THEN 'parked' ELSE 'failed' END,last_error=$4,available_at_utc=clock_timestamp()+least(interval '15 minutes',interval '5 seconds'*power(2,greatest(0,attempt-1))),lease_token=NULL,lease_expires_utc=NULL WHERE work_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.dispatch_full_dwell_refinement_v0_1(jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE q alias for $1; inserted text;
BEGIN
 IF jsonb_typeof(q)<>'object' OR jsonb_typeof(q->'windows')<>'array' OR jsonb_array_length(q->'windows') NOT BETWEEN 1 AND 1024 OR (q->'candidate_only')::boolean IS DISTINCT FROM true THEN RAISE EXCEPTION 'invalid refinement request' USING ERRCODE='22023'; END IF;
 INSERT INTO public.full_dwell_refinement_work_v0_1(timeline_analysis_id,recording_id,request_json) VALUES(q->'timeline_ref'->>'artifact_id',q->>'recording_id',q) ON CONFLICT DO NOTHING RETURNING timeline_analysis_id INTO inserted;
 IF inserted IS NOT NULL OR EXISTS(SELECT 1 FROM public.full_dwell_refinement_work_v0_1 w WHERE w.timeline_analysis_id=q->'timeline_ref'->>'artifact_id' AND w.request_json=q) THEN RETURN true; END IF;
 RAISE EXCEPTION 'refinement work identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.fail_full_dwell_refinement_dispatch_v0_1(text,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
UPDATE public.full_dwell_timeline_work_v0_1 SET last_error=$2 WHERE work_id=$1 AND state='succeeded' AND $2~'^[a-z0-9][a-z0-9._:-]{0,127}$' RETURNING true;$function$;

ALTER TABLE public.recording_full_dwell_timeline_v0_1 OWNER TO leo_routine_owner;
ALTER TABLE public.full_dwell_timeline_work_v0_1 OWNER TO leo_routine_owner;
ALTER TABLE public.full_dwell_refinement_work_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT,UPDATE ON public.recording_full_dwell_timeline_v0_1,public.full_dwell_timeline_work_v0_1,public.full_dwell_refinement_work_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.recording_full_dwell_timeline_v0_1,public.full_dwell_timeline_work_v0_1,public.full_dwell_refinement_work_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_recording_full_dwell_timeline_v0_1(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_recording_full_dwell_timeline_v0_1(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_full_dwell_timeline_v0_1(text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.admit_full_dwell_timeline_work_v0_1(jsonb,jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_full_dwell_timeline_work_v0_1(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.list_full_dwell_timeline_candidates_v0_1(integer) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_full_dwell_timeline_hardware_v0_1(text,bigint) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_full_dwell_timeline_work_v0_1(text,text,bigint,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_full_dwell_timeline_work_v0_1(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.dispatch_full_dwell_refinement_v0_1(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.fail_full_dwell_refinement_dispatch_v0_1(text,text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_recording_full_dwell_timeline_v0_1(jsonb),public.read_exact_recording_full_dwell_timeline_v0_1(text,text,text,text),public.read_latest_recording_full_dwell_timeline_v0_1(text),public.admit_full_dwell_timeline_work_v0_1(jsonb,jsonb),public.claim_full_dwell_timeline_work_v0_1(text,interval),public.list_full_dwell_timeline_candidates_v0_1(integer),public.read_full_dwell_timeline_hardware_v0_1(text,bigint),public.complete_full_dwell_timeline_work_v0_1(text,text,bigint,text,text),public.retry_full_dwell_timeline_work_v0_1(text,text,bigint,text),public.dispatch_full_dwell_refinement_v0_1(jsonb),public.fail_full_dwell_refinement_dispatch_v0_1(text,text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_full_dwell_timeline_v0_1(jsonb),public.read_exact_recording_full_dwell_timeline_v0_1(text,text,text,text),public.read_latest_recording_full_dwell_timeline_v0_1(text),public.admit_full_dwell_timeline_work_v0_1(jsonb,jsonb),public.claim_full_dwell_timeline_work_v0_1(text,interval),public.list_full_dwell_timeline_candidates_v0_1(integer),public.read_full_dwell_timeline_hardware_v0_1(text,bigint),public.complete_full_dwell_timeline_work_v0_1(text,text,bigint,text,text),public.retry_full_dwell_timeline_work_v0_1(text,text,bigint,text),public.dispatch_full_dwell_refinement_v0_1(jsonb),public.fail_full_dwell_refinement_dispatch_v0_1(text,text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_exact_recording_full_dwell_timeline_v0_1(text,text,text,text),public.read_latest_recording_full_dwell_timeline_v0_1(text) TO leo_dashboard;

COMMIT;
