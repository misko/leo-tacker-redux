BEGIN;

CREATE TABLE public.recording_starlink_pilot_refinement_v0_1 (
  analysis_id text PRIMARY KEY CHECK(analysis_id~'^slpr_[0-9a-f]{32}$'),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  recording_identity_digest_value text NOT NULL CHECK(recording_identity_digest_value~'^[0-9a-f]{64}$'),
  source_prescreen_analysis_id text NOT NULL REFERENCES public.recording_starlink_pilot_prescreen_v0_1(analysis_id),
  source_prescreen_bundle_digest_value text NOT NULL CHECK(source_prescreen_bundle_digest_value~'^[0-9a-f]{64}$'),
  source_suite_analysis_id text NOT NULL REFERENCES public.recording_starlink_detector_suite(analysis_id),
  source_suite_bundle_digest_value text NOT NULL CHECK(source_suite_bundle_digest_value~'^[0-9a-f]{64}$'),
  request_digest_value text NOT NULL CHECK(request_digest_value~'^[0-9a-f]{64}$'),
  bundle_digest_algorithm text NOT NULL CHECK(bundle_digest_algorithm='sha256'),
  bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
  stream_count integer NOT NULL CHECK(stream_count BETWEEN 1 AND 16),
  seed_count integer NOT NULL CHECK(seed_count BETWEEN stream_count AND stream_count*40),
  point_count integer NOT NULL CHECK(point_count=seed_count*8),
  idempotency_key text NOT NULL UNIQUE CHECK(idempotency_key<>''),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  UNIQUE(recording_id,recording_identity_digest_value,source_prescreen_analysis_id,source_prescreen_bundle_digest_value,source_suite_analysis_id,source_suite_bundle_digest_value,request_digest_value),
  FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);
CREATE INDEX recording_starlink_pilot_refinement_latest_v0_1_idx ON public.recording_starlink_pilot_refinement_v0_1(recording_id,published_at_utc DESC,analysis_id DESC);
CREATE TRIGGER recording_starlink_pilot_refinement_bundle_live BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value ON public.recording_starlink_pilot_refinement_v0_1 FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE TABLE public.starlink_pilot_refinement_work_v0_1 (
  source_prescreen_analysis_id text PRIMARY KEY REFERENCES public.recording_starlink_pilot_prescreen_v0_1(analysis_id),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  state text NOT NULL DEFAULT 'ready' CHECK(state IN('ready','leased','failed','succeeded','parked')),
  available_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),
  lease_token text,
  lease_generation bigint NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_expires_utc timestamptz,
  last_error text,
  result_analysis_id text REFERENCES public.recording_starlink_pilot_refinement_v0_1(analysis_id),
  result_bundle_digest_value text,
  completed_at_utc timestamptz,
  CHECK((state='leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL) OR (state<>'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)),
  CHECK((state='succeeded' AND result_analysis_id IS NOT NULL AND result_bundle_digest_value~'^[0-9a-f]{64}$' AND completed_at_utc IS NOT NULL) OR (state<>'succeeded' AND result_analysis_id IS NULL AND result_bundle_digest_value IS NULL AND completed_at_utc IS NULL))
);
CREATE INDEX starlink_pilot_refinement_work_claim_v0_1_idx ON public.starlink_pilot_refinement_work_v0_1(available_at_utc DESC,source_prescreen_analysis_id DESC) WHERE state IN('ready','failed','leased');

CREATE FUNCTION public.enqueue_starlink_pilot_refinement_work_v0_1() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 INSERT INTO public.starlink_pilot_refinement_work_v0_1(source_prescreen_analysis_id,recording_id) VALUES(NEW.analysis_id,NEW.recording_id) ON CONFLICT DO NOTHING;
 RETURN NEW;
END $function$;
CREATE TRIGGER recording_starlink_pilot_prescreen_enqueue_refinement_v0_1 AFTER INSERT ON public.recording_starlink_pilot_prescreen_v0_1 FOR EACH ROW EXECUTE FUNCTION public.enqueue_starlink_pilot_refinement_work_v0_1();
INSERT INTO public.starlink_pilot_refinement_work_v0_1(source_prescreen_analysis_id,recording_id) SELECT analysis_id,recording_id FROM public.recording_starlink_pilot_prescreen_v0_1 ON CONFLICT DO NOTHING;

CREATE FUNCTION public.claim_starlink_pilot_refinement_work_v0_1(text,interval)
RETURNS TABLE(source_prescreen_analysis_id text,recording_id text,lease_token text,lease_generation bigint,attempt integer,recording_manifest_digest_value text,data_digest_algorithm text,data_digest_value text,data_byte_count bigint,data_media_type text,data_format_id text,data_locator text,metadata_digest_algorithm text,metadata_digest_value text,metadata_byte_count bigint,metadata_media_type text,metadata_format_id text,metadata_locator text,prescreen_bundle_digest_algorithm text,prescreen_bundle_digest_value text,prescreen_bundle_byte_count bigint,prescreen_bundle_media_type text,prescreen_bundle_format_id text,prescreen_bundle_locator text,source_suite_analysis_id text,source_suite_request_digest_value text,suite_bundle_digest_algorithm text,suite_bundle_digest_value text,suite_bundle_byte_count bigint,suite_bundle_media_type text,suite_bundle_format_id text,suite_bundle_locator text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 IF $1='' OR $2<interval '1 second' OR $2>interval '8 hours' THEN RAISE EXCEPTION 'invalid pilot refinement claim' USING ERRCODE='22023'; END IF;
 RETURN QUERY WITH candidate AS (
  SELECT w.source_prescreen_analysis_id,s.analysis_id AS suite_id
  FROM public.starlink_pilot_refinement_work_v0_1 w
  JOIN LATERAL (SELECT x.analysis_id FROM public.recording_starlink_detector_suite x JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(x.bundle_digest_algorithm,x.bundle_digest_value) WHERE x.recording_id=w.recording_id AND x.result_state='candidates' AND o.lifecycle_state='live' ORDER BY x.published_at_utc DESC,x.analysis_id DESC LIMIT 1) s ON true
  WHERE w.attempt<3 AND w.available_at_utc<=clock_timestamp() AND (w.state IN('ready','failed') OR (w.state='leased' AND w.lease_expires_utc<=clock_timestamp()))
  ORDER BY w.available_at_utc DESC,w.source_prescreen_analysis_id DESC FOR UPDATE OF w SKIP LOCKED LIMIT 1
 ), claimed AS (
  UPDATE public.starlink_pilot_refinement_work_v0_1 w SET state='leased',attempt=w.attempt+1,lease_generation=w.lease_generation+1,lease_token=$1,lease_expires_utc=clock_timestamp()+$2,last_error=NULL FROM candidate c WHERE w.source_prescreen_analysis_id=c.source_prescreen_analysis_id RETURNING w.*,c.suite_id
 )
 SELECT c.source_prescreen_analysis_id,c.recording_id,c.lease_token,c.lease_generation,c.attempt,r.manifest_digest_value,r.data_digest_algorithm,r.data_digest_value,d.byte_count,d.media_type,d.format_id,d.locator,r.metadata_digest_algorithm,r.metadata_digest_value,m.byte_count,m.media_type,m.format_id,m.locator,p.bundle_digest_algorithm,p.bundle_digest_value,pb.byte_count,pb.media_type,pb.format_id,pb.locator,s.analysis_id,s.request_digest_value,s.bundle_digest_algorithm,s.bundle_digest_value,sb.byte_count,sb.media_type,sb.format_id,sb.locator
 FROM claimed c JOIN public.recording r ON r.recording_id=c.recording_id JOIN public.object_blob d ON (d.digest_algorithm,d.digest_value)=(r.data_digest_algorithm,r.data_digest_value) JOIN public.object_blob m ON (m.digest_algorithm,m.digest_value)=(r.metadata_digest_algorithm,r.metadata_digest_value) JOIN public.recording_starlink_pilot_prescreen_v0_1 p ON p.analysis_id=c.source_prescreen_analysis_id JOIN public.object_blob pb ON (pb.digest_algorithm,pb.digest_value)=(p.bundle_digest_algorithm,p.bundle_digest_value) JOIN public.recording_starlink_detector_suite s ON s.analysis_id=c.suite_id JOIN public.object_blob sb ON (sb.digest_algorithm,sb.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value) WHERE d.lifecycle_state='live' AND m.lifecycle_state='live' AND pb.lifecycle_state='live' AND sb.lifecycle_state='live';
END $function$;

CREATE FUNCTION public.publish_recording_starlink_pilot_refinement_v0_1(jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE p alias for $1; inserted text;
BEGIN
 IF jsonb_typeof(p)<>'object' OR (p->>'stream_count')::integer NOT BETWEEN 1 AND 16 OR (p->>'seed_count')::integer NOT BETWEEN (p->>'stream_count')::integer AND (p->>'stream_count')::integer*40 OR (p->>'point_count')::integer<>(p->>'seed_count')::integer*8 THEN RAISE EXCEPTION 'invalid pilot refinement publication' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM public.recording_starlink_pilot_prescreen_v0_1 q JOIN public.recording_starlink_detector_suite s ON s.recording_id=q.recording_id WHERE q.analysis_id=p->>'source_prescreen_analysis_id' AND q.recording_id=p->>'recording_id' AND q.recording_identity_digest_value=p->>'recording_identity_digest_value' AND q.bundle_digest_value=p->>'source_prescreen_bundle_digest_value' AND s.analysis_id=p->>'source_suite_analysis_id' AND s.input_recording_digest_value=p->>'recording_identity_digest_value' AND s.bundle_digest_value=p->>'source_suite_bundle_digest_value';
 IF NOT FOUND THEN RAISE EXCEPTION 'pilot refinement source closure differs' USING ERRCODE='23503'; END IF;
 INSERT INTO public.recording_starlink_pilot_refinement_v0_1 VALUES(p->>'analysis_id',p->>'recording_id',p->>'recording_identity_digest_value',p->>'source_prescreen_analysis_id',p->>'source_prescreen_bundle_digest_value',p->>'source_suite_analysis_id',p->>'source_suite_bundle_digest_value',p->>'request_digest_value','sha256',p->>'bundle_digest_value',(p->>'stream_count')::integer,(p->>'seed_count')::integer,(p->>'point_count')::integer,p->>'idempotency_key',DEFAULT) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
 IF inserted IS NOT NULL THEN RETURN true; END IF;
 IF EXISTS(SELECT 1 FROM public.recording_starlink_pilot_refinement_v0_1 e WHERE e.analysis_id=p->>'analysis_id' AND e.recording_id=p->>'recording_id' AND e.recording_identity_digest_value=p->>'recording_identity_digest_value' AND e.source_prescreen_analysis_id=p->>'source_prescreen_analysis_id' AND e.source_prescreen_bundle_digest_value=p->>'source_prescreen_bundle_digest_value' AND e.source_suite_analysis_id=p->>'source_suite_analysis_id' AND e.source_suite_bundle_digest_value=p->>'source_suite_bundle_digest_value' AND e.request_digest_value=p->>'request_digest_value' AND e.bundle_digest_value=p->>'bundle_digest_value' AND e.stream_count=(p->>'stream_count')::integer AND e.seed_count=(p->>'seed_count')::integer AND e.point_count=(p->>'point_count')::integer AND e.idempotency_key=p->>'idempotency_key') THEN RETURN true; END IF;
 RAISE EXCEPTION 'pilot refinement catalog identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.complete_starlink_pilot_refinement_work_v0_1(text,text,bigint,text,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.starlink_pilot_refinement_work_v0_1 w SET state='succeeded',result_analysis_id=$4,result_bundle_digest_value=$5,completed_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL WHERE w.source_prescreen_analysis_id=$1 AND w.state='leased' AND w.lease_token=$2 AND w.lease_generation=$3 AND w.lease_expires_utc>clock_timestamp() AND EXISTS(SELECT 1 FROM public.recording_starlink_pilot_refinement_v0_1 p WHERE p.analysis_id=$4 AND p.recording_id=w.recording_id AND p.source_prescreen_analysis_id=w.source_prescreen_analysis_id AND p.bundle_digest_value=$5) RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.retry_starlink_pilot_refinement_work_v0_1(text,text,bigint,text) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.starlink_pilot_refinement_work_v0_1 SET state=CASE WHEN attempt>=3 THEN 'parked' ELSE 'failed' END,last_error=$4,available_at_utc=clock_timestamp()+least(interval '15 minutes',interval '5 seconds'*power(2,greatest(0,attempt-1))),lease_token=NULL,lease_expires_utc=NULL WHERE source_prescreen_analysis_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.read_exact_recording_starlink_pilot_refinement_v0_1(text,text,text,text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,source_prescreen_analysis_id text,source_prescreen_bundle_digest_value text,source_suite_analysis_id text,source_suite_bundle_digest_value text,request_digest_value text,stream_count integer,seed_count integer,point_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.source_prescreen_analysis_id,h.source_prescreen_bundle_digest_value,h.source_suite_analysis_id,h.source_suite_bundle_digest_value,h.request_digest_value,h.stream_count,h.seed_count,h.point_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_starlink_pilot_refinement_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.analysis_id=$1 AND h.recording_id=$2 AND h.bundle_digest_algorithm=$3 AND h.bundle_digest_value=$4 AND o.lifecycle_state='live';$function$;

CREATE FUNCTION public.read_latest_recording_starlink_pilot_refinement_v0_1(text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,source_prescreen_analysis_id text,source_prescreen_bundle_digest_value text,source_suite_analysis_id text,source_suite_bundle_digest_value text,request_digest_value text,stream_count integer,seed_count integer,point_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.source_prescreen_analysis_id,h.source_prescreen_bundle_digest_value,h.source_suite_analysis_id,h.source_suite_bundle_digest_value,h.request_digest_value,h.stream_count,h.seed_count,h.point_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_starlink_pilot_refinement_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.recording_id=$1 AND o.lifecycle_state='live' ORDER BY h.published_at_utc DESC,h.analysis_id DESC LIMIT 1;$function$;

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
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_full_dwell_timeline_v0_1.bundle',analysis_id::text FROM public.recording_full_dwell_timeline_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_adaptive_response_v0_1.bundle',analysis_id::text FROM public.recording_starlink_adaptive_response_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_adaptive_qam_v0_4.bundle',analysis_id::text FROM public.recording_starlink_adaptive_qam_v0_4
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_prescreen_v0_1.bundle',analysis_id::text FROM public.recording_starlink_pilot_prescreen_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_refinement_v0_1.bundle',analysis_id::text FROM public.recording_starlink_pilot_refinement_v0_1;

ALTER TABLE public.recording_starlink_pilot_refinement_v0_1 OWNER TO leo_routine_owner;
ALTER TABLE public.starlink_pilot_refinement_work_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.recording_starlink_pilot_refinement_v0_1,public.starlink_pilot_refinement_work_v0_1 TO leo_routine_owner;
GRANT UPDATE ON public.starlink_pilot_refinement_work_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.recording_starlink_pilot_refinement_v0_1,public.starlink_pilot_refinement_work_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.enqueue_starlink_pilot_refinement_work_v0_1() OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_starlink_pilot_refinement_work_v0_1(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_recording_starlink_pilot_refinement_v0_1(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_starlink_pilot_refinement_work_v0_1(text,text,bigint,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_starlink_pilot_refinement_work_v0_1(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_recording_starlink_pilot_refinement_v0_1(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_starlink_pilot_refinement_v0_1(text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.enqueue_starlink_pilot_refinement_work_v0_1(),public.claim_starlink_pilot_refinement_work_v0_1(text,interval),public.publish_recording_starlink_pilot_refinement_v0_1(jsonb),public.complete_starlink_pilot_refinement_work_v0_1(text,text,bigint,text,text),public.retry_starlink_pilot_refinement_work_v0_1(text,text,bigint,text),public.read_exact_recording_starlink_pilot_refinement_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_refinement_v0_1(text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.claim_starlink_pilot_refinement_work_v0_1(text,interval),public.publish_recording_starlink_pilot_refinement_v0_1(jsonb),public.complete_starlink_pilot_refinement_work_v0_1(text,text,bigint,text,text),public.retry_starlink_pilot_refinement_work_v0_1(text,text,bigint,text),public.read_exact_recording_starlink_pilot_refinement_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_refinement_v0_1(text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_exact_recording_starlink_pilot_refinement_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_refinement_v0_1(text) TO leo_dashboard;

COMMIT;
