BEGIN;

CREATE TABLE public.recording_starlink_pilot_prescreen_v0_1 (
  analysis_id text PRIMARY KEY CHECK(analysis_id~'^slps_[0-9a-f]{32}$'),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  recording_identity_digest_value text NOT NULL CHECK(recording_identity_digest_value~'^[0-9a-f]{64}$'),
  request_digest_value text NOT NULL CHECK(request_digest_value~'^[0-9a-f]{64}$'),
  bundle_digest_algorithm text NOT NULL CHECK(bundle_digest_algorithm='sha256'),
  bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
  stream_count integer NOT NULL CHECK(stream_count BETWEEN 1 AND 16),
  window_count integer NOT NULL CHECK(window_count BETWEEN stream_count AND stream_count*100000),
  analyzed_sample_count bigint NOT NULL CHECK(analyzed_sample_count>0),
  selected_window_count integer NOT NULL CHECK(selected_window_count BETWEEN stream_count AND window_count),
  idempotency_key text NOT NULL UNIQUE CHECK(idempotency_key<>''),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  UNIQUE(recording_id,recording_identity_digest_value,request_digest_value),
  FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);
CREATE INDEX recording_starlink_pilot_prescreen_latest_v0_1_idx ON public.recording_starlink_pilot_prescreen_v0_1(recording_id,published_at_utc DESC,analysis_id DESC);
CREATE TRIGGER recording_starlink_pilot_prescreen_bundle_live BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value ON public.recording_starlink_pilot_prescreen_v0_1 FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE FUNCTION public.publish_recording_starlink_pilot_prescreen_v0_1(jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE p alias for $1; inserted text;
BEGIN
 IF jsonb_typeof(p)<>'object' OR (p->>'stream_count')::integer NOT BETWEEN 1 AND 16 OR (p->>'window_count')::integer NOT BETWEEN (p->>'stream_count')::integer AND (p->>'stream_count')::integer*100000 OR (p->>'analyzed_sample_count')::bigint<=0 OR (p->>'selected_window_count')::integer NOT BETWEEN (p->>'stream_count')::integer AND (p->>'window_count')::integer THEN RAISE EXCEPTION 'invalid pilot prescreen publication' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM public.recording r JOIN public.object_blob d ON (d.digest_algorithm,d.digest_value)=(r.data_digest_algorithm,r.data_digest_value) JOIN public.object_blob m ON (m.digest_algorithm,m.digest_value)=(r.metadata_digest_algorithm,r.metadata_digest_value) WHERE r.recording_id=p->>'recording_id' AND r.data_digest_value=p->>'data_digest_value' AND r.metadata_digest_value=p->>'metadata_digest_value' AND r.manifest_digest_value=p->>'manifest_digest_value' AND d.lifecycle_state='live' AND m.lifecycle_state='live';
 IF NOT FOUND THEN RAISE EXCEPTION 'pilot prescreen recording closure differs' USING ERRCODE='23503'; END IF;
 INSERT INTO public.recording_starlink_pilot_prescreen_v0_1 VALUES(p->>'analysis_id',p->>'recording_id',p->>'recording_identity_digest_value',p->>'request_digest_value','sha256',p->>'bundle_digest_value',(p->>'stream_count')::integer,(p->>'window_count')::integer,(p->>'analyzed_sample_count')::bigint,(p->>'selected_window_count')::integer,p->>'idempotency_key',DEFAULT) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
 IF inserted IS NOT NULL THEN RETURN true; END IF;
 IF EXISTS(SELECT 1 FROM public.recording_starlink_pilot_prescreen_v0_1 e WHERE e.analysis_id=p->>'analysis_id' AND e.recording_id=p->>'recording_id' AND e.recording_identity_digest_value=p->>'recording_identity_digest_value' AND e.request_digest_value=p->>'request_digest_value' AND e.bundle_digest_value=p->>'bundle_digest_value' AND e.stream_count=(p->>'stream_count')::integer AND e.window_count=(p->>'window_count')::integer AND e.analyzed_sample_count=(p->>'analyzed_sample_count')::bigint AND e.selected_window_count=(p->>'selected_window_count')::integer AND e.idempotency_key=p->>'idempotency_key') THEN RETURN true; END IF;
 RAISE EXCEPTION 'pilot prescreen catalog identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.read_exact_recording_starlink_pilot_prescreen_v0_1(text,text,text,text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,request_digest_value text,stream_count integer,window_count integer,analyzed_sample_count bigint,selected_window_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.request_digest_value,h.stream_count,h.window_count,h.analyzed_sample_count,h.selected_window_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_starlink_pilot_prescreen_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.analysis_id=$1 AND h.recording_id=$2 AND h.bundle_digest_algorithm=$3 AND h.bundle_digest_value=$4 AND o.lifecycle_state='live';$function$;

CREATE FUNCTION public.read_latest_recording_starlink_pilot_prescreen_v0_1(text)
RETURNS TABLE(analysis_id text,recording_id text,recording_identity_digest_value text,request_digest_value text,stream_count integer,window_count integer,analyzed_sample_count bigint,selected_window_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,h.request_digest_value,h.stream_count,h.window_count,h.analyzed_sample_count,h.selected_window_count,h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_starlink_pilot_prescreen_v0_1 h JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE h.recording_id=$1 AND o.lifecycle_state='live' ORDER BY h.published_at_utc DESC,h.analysis_id DESC LIMIT 1;$function$;

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
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_prescreen_v0_1.bundle',analysis_id::text FROM public.recording_starlink_pilot_prescreen_v0_1;

ALTER TABLE public.recording_starlink_pilot_prescreen_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.recording_starlink_pilot_prescreen_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.recording_starlink_pilot_prescreen_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_recording_starlink_pilot_prescreen_v0_1(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_recording_starlink_pilot_prescreen_v0_1(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_starlink_pilot_prescreen_v0_1(text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_recording_starlink_pilot_prescreen_v0_1(jsonb),public.read_exact_recording_starlink_pilot_prescreen_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_prescreen_v0_1(text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_starlink_pilot_prescreen_v0_1(jsonb),public.read_exact_recording_starlink_pilot_prescreen_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_prescreen_v0_1(text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_exact_recording_starlink_pilot_prescreen_v0_1(text,text,text,text),public.read_latest_recording_starlink_pilot_prescreen_v0_1(text) TO leo_dashboard;

COMMIT;
