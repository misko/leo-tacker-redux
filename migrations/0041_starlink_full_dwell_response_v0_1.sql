BEGIN;

CREATE TABLE public.recording_starlink_full_dwell_v0_1 (
  analysis_id text PRIMARY KEY CHECK (analysis_id ~ '^slfd_[0-9a-f]{32}$'),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  input_recording_digest_algorithm text NOT NULL CHECK (input_recording_digest_algorithm='sha256'),
  input_recording_digest_value text NOT NULL CHECK (input_recording_digest_value ~ '^[0-9a-f]{64}$'),
  source_suite_analysis_id text NOT NULL REFERENCES public.recording_starlink_detector_suite(analysis_id),
  source_suite_bundle_digest_algorithm text NOT NULL CHECK (source_suite_bundle_digest_algorithm='sha256'),
  source_suite_bundle_digest_value text NOT NULL CHECK (source_suite_bundle_digest_value ~ '^[0-9a-f]{64}$'),
  source_suite_request_digest_algorithm text NOT NULL CHECK (source_suite_request_digest_algorithm='sha256'),
  source_suite_request_digest_value text NOT NULL CHECK (source_suite_request_digest_value ~ '^[0-9a-f]{64}$'),
  request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm='sha256'),
  request_digest_value text NOT NULL CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
  bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm='sha256'),
  bundle_digest_value text NOT NULL CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
  stream_count integer NOT NULL CHECK (stream_count BETWEEN 1 AND 16),
  prescreen_window_count integer NOT NULL CHECK (prescreen_window_count BETWEEN stream_count AND stream_count*16384),
  exact_window_count integer NOT NULL CHECK (exact_window_count BETWEEN stream_count AND stream_count*511),
  point_count integer NOT NULL CHECK (point_count=exact_window_count*8),
  idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key<>''),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  UNIQUE(analysis_id,recording_id),
  UNIQUE(recording_id,input_recording_digest_algorithm,input_recording_digest_value,
         source_suite_analysis_id,source_suite_bundle_digest_algorithm,source_suite_bundle_digest_value,
         source_suite_request_digest_algorithm,source_suite_request_digest_value,
         request_digest_algorithm,request_digest_value),
  FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE TABLE public.recording_starlink_full_dwell_point_v0_1 (
  analysis_id text NOT NULL REFERENCES public.recording_starlink_full_dwell_v0_1(analysis_id),
  recording_id text NOT NULL,
  segment_id text NOT NULL,
  radio_id text NOT NULL,
  receiver_chain_id text NOT NULL,
  channel_number integer NOT NULL CHECK (channel_number BETWEEN 1 AND 4),
  edge text NOT NULL CHECK (edge IN ('lower','upper')),
  method text NOT NULL CHECK (method IN ('anchor-8','differential-16','differential-32','glrt-32','glrt-64','full-frame-acquire','full-frame-verify','full-frame-full')),
  window_index integer NOT NULL CHECK (window_index>=0),
  start_sample bigint NOT NULL CHECK (start_sample>=0),
  stop_sample bigint NOT NULL CHECK (stop_sample>start_sample),
  interval_start_utc_ns bigint NOT NULL CHECK (interval_start_utc_ns>=0),
  interval_stop_utc_ns bigint NOT NULL CHECK (interval_stop_utc_ns>interval_start_utc_ns),
  prescreen_score double precision NOT NULL CHECK (prescreen_score>=0 AND prescreen_score<='Infinity'),
  qin_score double precision NOT NULL CHECK (qin_score BETWEEN 0 AND 1),
  qin_winning_epoch_sample_in_segment bigint NOT NULL,
  qin_winning_coarse_cfo_hz double precision NOT NULL,
  qin_winning_residual_cfo_hz double precision NOT NULL,
  surrogate_scores double precision[] NOT NULL CHECK (cardinality(surrogate_scores) BETWEEN 1 AND 32),
  surrogate_winners jsonb NOT NULL CHECK (jsonb_typeof(surrogate_winners)='array'),
  finite_upper_tail_rank integer NOT NULL CHECK (finite_upper_tail_rank BETWEEN 1 AND 33),
  qin_minus_max_surrogate double precision NOT NULL,
  dependence_group text NOT NULL CHECK (dependence_group<>''),
  PRIMARY KEY(analysis_id,recording_id,segment_id,radio_id,receiver_chain_id,edge,method,window_index),
  FOREIGN KEY(analysis_id,recording_id) REFERENCES public.recording_starlink_full_dwell_v0_1(analysis_id,recording_id)
);

CREATE INDEX recording_starlink_full_dwell_latest_v0_1_idx ON public.recording_starlink_full_dwell_v0_1(recording_id,published_at_utc DESC,analysis_id DESC);
CREATE INDEX recording_starlink_full_dwell_point_time_v0_1_idx ON public.recording_starlink_full_dwell_point_v0_1(recording_id,interval_start_utc_ns,radio_id,receiver_chain_id,method);
CREATE TRIGGER recording_starlink_full_dwell_bundle_must_be_live BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value ON public.recording_starlink_full_dwell_v0_1 FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

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
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_full_dwell_v0_1.bundle',analysis_id::text FROM public.recording_starlink_full_dwell_v0_1;

CREATE FUNCTION public.publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE p alias for $1; rows alias for $2; inserted text;
BEGIN
  IF jsonb_typeof(p)<>'object' OR jsonb_typeof(rows)<>'array' OR jsonb_array_length(rows)>262144 OR jsonb_array_length(rows)<>(p->>'point_count')::integer THEN RAISE EXCEPTION 'invalid or unbounded full-dwell publication' USING ERRCODE='22023'; END IF;
  PERFORM 1 FROM public.recording_starlink_detector_suite s WHERE s.analysis_id=p->>'source_suite_analysis_id' AND s.recording_id=p->>'recording_id' AND s.input_recording_digest_value=p->>'input_recording_digest_value' AND s.bundle_digest_value=p->>'source_suite_bundle_digest_value' AND s.request_digest_value=p->>'source_suite_request_digest_value';
  IF NOT FOUND THEN RAISE EXCEPTION 'source detector-suite identity is not authoritative' USING ERRCODE='23503'; END IF;
  INSERT INTO public.recording_starlink_full_dwell_v0_1 VALUES(p->>'analysis_id',p->>'recording_id','sha256',p->>'input_recording_digest_value',p->>'source_suite_analysis_id','sha256',p->>'source_suite_bundle_digest_value','sha256',p->>'source_suite_request_digest_value','sha256',p->>'request_digest_value','sha256',p->>'bundle_digest_value',(p->>'stream_count')::integer,(p->>'prescreen_window_count')::integer,(p->>'exact_window_count')::integer,(p->>'point_count')::integer,p->>'idempotency_key',DEFAULT) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
  IF inserted IS NULL THEN
    IF EXISTS(SELECT 1 FROM public.recording_starlink_full_dwell_v0_1 e WHERE to_jsonb(e)-'published_at_utc'=p) THEN RETURN true; END IF;
    RAISE EXCEPTION 'full-dwell catalog identity conflict' USING ERRCODE='23505';
  END IF;
  INSERT INTO public.recording_starlink_full_dwell_point_v0_1 SELECT p->>'analysis_id',p->>'recording_id',x.segment_id,x.radio_id,x.receiver_chain_id,x.channel_number,x.edge,x.method,x.window_index,x.start_sample,x.stop_sample,x.interval_start_utc_ns,x.interval_stop_utc_ns,x.prescreen_score,x.qin_score,x.qin_winning_epoch_sample_in_segment,x.qin_winning_coarse_cfo_hz,x.qin_winning_residual_cfo_hz,x.surrogate_scores,x.surrogate_winners,x.finite_upper_tail_rank,x.qin_minus_max_surrogate,x.dependence_group FROM jsonb_to_recordset(rows) AS x(segment_id text,radio_id text,receiver_chain_id text,channel_number integer,edge text,method text,window_index integer,start_sample bigint,stop_sample bigint,interval_start_utc_ns bigint,interval_stop_utc_ns bigint,prescreen_score double precision,qin_score double precision,qin_winning_epoch_sample_in_segment bigint,qin_winning_coarse_cfo_hz double precision,qin_winning_residual_cfo_hz double precision,surrogate_scores double precision[],surrogate_winners jsonb,finite_upper_tail_rank integer,qin_minus_max_surrogate double precision,dependence_group text);
  RETURN true;
END $function$;

CREATE FUNCTION public.read_exact_recording_starlink_full_dwell_v0_1(text,text,text,text)
RETURNS TABLE(analysis_id text,recording_id text,input_recording_digest_value text,
 source_suite_analysis_id text,source_suite_bundle_digest_value text,
 source_suite_request_digest_value text,request_digest_value text,stream_count integer,
 prescreen_window_count integer,exact_window_count integer,point_count integer,
 bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,
 bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.input_recording_digest_value,
 h.source_suite_analysis_id,h.source_suite_bundle_digest_value,
 h.source_suite_request_digest_value,h.request_digest_value,h.stream_count,
 h.prescreen_window_count,h.exact_window_count,h.point_count,
 h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator
FROM public.recording_starlink_full_dwell_v0_1 h JOIN public.object_blob o
 ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value)
WHERE h.analysis_id=$1 AND h.recording_id=$2 AND h.bundle_digest_algorithm=$3
 AND h.bundle_digest_value=$4 AND o.lifecycle_state='live'; $function$;

CREATE FUNCTION public.read_latest_recording_starlink_full_dwell_v0_1(text)
RETURNS TABLE(analysis_id text,recording_id text,bundle_digest_algorithm text,bundle_digest_value text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT h.analysis_id,h.recording_id,h.bundle_digest_algorithm,h.bundle_digest_value
FROM public.recording_starlink_full_dwell_v0_1 h JOIN public.object_blob o
 ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value)
WHERE h.recording_id=$1 AND o.lifecycle_state='live'
ORDER BY h.published_at_utc DESC,h.analysis_id DESC LIMIT 1; $function$;

CREATE FUNCTION public.read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer) RETURNS SETOF public.recording_starlink_full_dwell_point_v0_1
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 IF $6 NOT BETWEEN 1 AND 4096 OR cardinality($2)>8 OR cardinality($3)>64 OR cardinality($4)>32 OR cardinality($5)>2 THEN RAISE EXCEPTION 'invalid full-dwell query bound' USING ERRCODE='22023'; END IF;
 RETURN QUERY SELECT p.* FROM public.recording_starlink_full_dwell_point_v0_1 p JOIN public.recording_starlink_full_dwell_v0_1 h USING(analysis_id,recording_id) JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value) WHERE p.recording_id=$1 AND o.lifecycle_state='live' AND (cardinality($2)=0 OR p.method=ANY($2)) AND (cardinality($3)=0 OR p.radio_id=ANY($3)) AND (cardinality($4)=0 OR p.receiver_chain_id=ANY($4)) AND (cardinality($5)=0 OR p.edge=ANY($5)) ORDER BY h.published_at_utc DESC,p.interval_start_utc_ns,p.segment_id,p.radio_id,p.receiver_chain_id,p.edge,p.method LIMIT $6;
END $function$;

ALTER TABLE public.recording_starlink_full_dwell_v0_1 OWNER TO leo_routine_owner;
ALTER TABLE public.recording_starlink_full_dwell_point_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.recording_starlink_full_dwell_v0_1,public.recording_starlink_full_dwell_point_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.recording_starlink_full_dwell_v0_1,public.recording_starlink_full_dwell_point_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_recording_starlink_full_dwell_v0_1(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_starlink_full_dwell_v0_1(text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb),public.read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer),public.read_exact_recording_starlink_full_dwell_v0_1(text,text,text,text),public.read_latest_recording_starlink_full_dwell_v0_1(text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_starlink_full_dwell_v0_1(jsonb,jsonb) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_recording_starlink_full_dwell_v0_1(text,text[],text[],text[],text[],integer),public.read_exact_recording_starlink_full_dwell_v0_1(text,text,text,text),public.read_latest_recording_starlink_full_dwell_v0_1(text) TO leo_dashboard;
GRANT EXECUTE ON FUNCTION public.read_exact_recording_starlink_full_dwell_v0_1(text,text,text,text),public.read_latest_recording_starlink_full_dwell_v0_1(text) TO leo_analysis;

COMMIT;
