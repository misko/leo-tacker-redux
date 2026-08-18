BEGIN;

CREATE TABLE public.recording_receiver_agnostic_cfo_qam_v0_6 (
  analysis_id text PRIMARY KEY CHECK(analysis_id~'^slcfoqam6rec_[0-9a-f]{32}$'),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  recording_identity_digest_value text NOT NULL CHECK(recording_identity_digest_value~'^[0-9a-f]{64}$'),
  request_digest_value text NOT NULL CHECK(request_digest_value~'^[0-9a-f]{64}$'),
  bundle_digest_algorithm text NOT NULL CHECK(bundle_digest_algorithm='sha256'),
  bundle_digest_value text NOT NULL CHECK(bundle_digest_value~'^[0-9a-f]{64}$'),
  stream_count integer NOT NULL CHECK(stream_count BETWEEN 1 AND 2),
  window_count integer NOT NULL CHECK(window_count BETWEEN stream_count AND 6),
  pattern_evidence_count integer NOT NULL CHECK(pattern_evidence_count BETWEEN window_count AND window_count*9 AND pattern_evidence_count<=54),
  unique_cell_count integer NOT NULL CHECK(unique_cell_count BETWEEN window_count AND window_count*150000),
  pattern_evaluation_count integer NOT NULL CHECK(pattern_evaluation_count BETWEEN unique_cell_count AND unique_cell_count*9 AND pattern_evaluation_count<=window_count*1000000),
  candidates_only boolean NOT NULL CHECK(candidates_only),
  idempotency_key text NOT NULL UNIQUE CHECK(idempotency_key<>''),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  UNIQUE(recording_id,recording_identity_digest_value,request_digest_value),
  FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE INDEX recording_receiver_agnostic_cfo_qam_latest_v0_6_idx
ON public.recording_receiver_agnostic_cfo_qam_v0_6(recording_id,published_at_utc DESC,analysis_id DESC);

CREATE TRIGGER recording_receiver_agnostic_cfo_qam_bundle_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value
ON public.recording_receiver_agnostic_cfo_qam_v0_6
FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE FUNCTION public.publish_recording_receiver_agnostic_cfo_qam_v0_6(jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE p alias for $1; inserted text;
BEGIN
  IF jsonb_typeof(p)<>'object'
    OR (p->>'analysis_id')!~'^slcfoqam6rec_[0-9a-f]{32}$'
    OR (p->>'recording_identity_digest_value')!~'^[0-9a-f]{64}$'
    OR (p->>'request_digest_value')!~'^[0-9a-f]{64}$'
    OR (p->>'bundle_digest_value')!~'^[0-9a-f]{64}$'
    OR (p->>'stream_count')::integer NOT BETWEEN 1 AND 2
    OR (p->>'window_count')::integer NOT BETWEEN (p->>'stream_count')::integer AND 6
    OR (p->>'pattern_evidence_count')::integer NOT BETWEEN (p->>'window_count')::integer AND (p->>'window_count')::integer*9
    OR (p->>'pattern_evidence_count')::integer>54
    OR (p->>'unique_cell_count')::integer NOT BETWEEN (p->>'window_count')::integer AND (p->>'window_count')::integer*150000
    OR (p->>'pattern_evaluation_count')::integer NOT BETWEEN (p->>'unique_cell_count')::integer AND (p->>'unique_cell_count')::integer*9
    OR (p->>'pattern_evaluation_count')::integer>(p->>'window_count')::integer*1000000
    OR (p->>'candidates_only')::boolean IS DISTINCT FROM true
    OR coalesce(p->>'idempotency_key','')=''
  THEN
    RAISE EXCEPTION 'invalid receiver-agnostic CFO/QAM publication' USING ERRCODE='22023';
  END IF;
  PERFORM 1 FROM public.recording r
  JOIN public.object_blob data_object
    ON (data_object.digest_algorithm,data_object.digest_value)=
       (r.data_digest_algorithm,r.data_digest_value)
  JOIN public.object_blob metadata_object
    ON (metadata_object.digest_algorithm,metadata_object.digest_value)=
       (r.metadata_digest_algorithm,r.metadata_digest_value)
  WHERE r.recording_id=p->>'recording_id' AND r.state='published'
    AND data_object.lifecycle_state='live'
    AND metadata_object.lifecycle_state='live';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'receiver-agnostic CFO/QAM recording is unavailable' USING ERRCODE='23503';
  END IF;
  INSERT INTO public.recording_receiver_agnostic_cfo_qam_v0_6(
    analysis_id,recording_id,recording_identity_digest_value,request_digest_value,
    bundle_digest_algorithm,bundle_digest_value,stream_count,window_count,
    pattern_evidence_count,unique_cell_count,pattern_evaluation_count,
    candidates_only,idempotency_key
  ) VALUES(
    p->>'analysis_id',p->>'recording_id',p->>'recording_identity_digest_value',
    p->>'request_digest_value','sha256',p->>'bundle_digest_value',
    (p->>'stream_count')::integer,(p->>'window_count')::integer,
    (p->>'pattern_evidence_count')::integer,(p->>'unique_cell_count')::integer,
    (p->>'pattern_evaluation_count')::integer,true,p->>'idempotency_key'
  ) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
  IF inserted IS NOT NULL THEN RETURN true; END IF;
  IF EXISTS(
    SELECT 1 FROM public.recording_receiver_agnostic_cfo_qam_v0_6 e
    WHERE e.analysis_id=p->>'analysis_id' AND e.recording_id=p->>'recording_id'
      AND e.recording_identity_digest_value=p->>'recording_identity_digest_value'
      AND e.request_digest_value=p->>'request_digest_value'
      AND e.bundle_digest_algorithm='sha256'
      AND e.bundle_digest_value=p->>'bundle_digest_value'
      AND e.stream_count=(p->>'stream_count')::integer
      AND e.window_count=(p->>'window_count')::integer
      AND e.pattern_evidence_count=(p->>'pattern_evidence_count')::integer
      AND e.unique_cell_count=(p->>'unique_cell_count')::integer
      AND e.pattern_evaluation_count=(p->>'pattern_evaluation_count')::integer
      AND e.candidates_only AND e.idempotency_key=p->>'idempotency_key'
  ) THEN RETURN true; END IF;
  RAISE EXCEPTION 'receiver-agnostic CFO/QAM catalog identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.read_exact_recording_receiver_agnostic_cfo_qam_v0_6(text,text,text,text)
RETURNS TABLE(
  analysis_id text,recording_id text,recording_identity_digest_value text,
  request_digest_value text,stream_count integer,window_count integer,
  pattern_evidence_count integer,unique_cell_count integer,
  pattern_evaluation_count integer,candidates_only boolean,
  bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,
  bundle_media_type text,bundle_format_id text,bundle_locator text
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,
  h.request_digest_value,h.stream_count,h.window_count,h.pattern_evidence_count,
  h.unique_cell_count,h.pattern_evaluation_count,h.candidates_only,
  h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,
  o.format_id,o.locator
FROM public.recording_receiver_agnostic_cfo_qam_v0_6 h
JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value)
WHERE h.analysis_id=$1 AND h.recording_id=$2 AND h.bundle_digest_algorithm=$3
  AND h.bundle_digest_value=$4 AND o.lifecycle_state='live';
$function$;

CREATE FUNCTION public.read_latest_recording_receiver_agnostic_cfo_qam_v0_6(text)
RETURNS TABLE(
  analysis_id text,recording_id text,recording_identity_digest_value text,
  request_digest_value text,stream_count integer,window_count integer,
  pattern_evidence_count integer,unique_cell_count integer,
  pattern_evaluation_count integer,candidates_only boolean,
  bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,
  bundle_media_type text,bundle_format_id text,bundle_locator text
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
SELECT h.analysis_id,h.recording_id,h.recording_identity_digest_value,
  h.request_digest_value,h.stream_count,h.window_count,h.pattern_evidence_count,
  h.unique_cell_count,h.pattern_evaluation_count,h.candidates_only,
  h.bundle_digest_algorithm,h.bundle_digest_value,o.byte_count,o.media_type,
  o.format_id,o.locator
FROM public.recording_receiver_agnostic_cfo_qam_v0_6 h
JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(h.bundle_digest_algorithm,h.bundle_digest_value)
WHERE h.recording_id=$1 AND o.lifecycle_state='live'
ORDER BY h.published_at_utc DESC,h.analysis_id DESC LIMIT 1;
$function$;

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
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_refinement_v0_1.bundle',analysis_id::text FROM public.recording_starlink_pilot_refinement_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_symbolwise_replay_v0_1.bundle',analysis_id::text FROM public.recording_starlink_symbolwise_replay_v0_1
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_receiver_agnostic_cfo_qam_v0_6.bundle',analysis_id::text FROM public.recording_receiver_agnostic_cfo_qam_v0_6;

ALTER TABLE public.recording_receiver_agnostic_cfo_qam_v0_6 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.recording_receiver_agnostic_cfo_qam_v0_6 TO leo_routine_owner;
REVOKE ALL ON public.recording_receiver_agnostic_cfo_qam_v0_6 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_recording_receiver_agnostic_cfo_qam_v0_6(jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_recording_receiver_agnostic_cfo_qam_v0_6(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_receiver_agnostic_cfo_qam_v0_6(text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_recording_receiver_agnostic_cfo_qam_v0_6(jsonb),public.read_exact_recording_receiver_agnostic_cfo_qam_v0_6(text,text,text,text),public.read_latest_recording_receiver_agnostic_cfo_qam_v0_6(text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_receiver_agnostic_cfo_qam_v0_6(jsonb),public.read_exact_recording_receiver_agnostic_cfo_qam_v0_6(text,text,text,text),public.read_latest_recording_receiver_agnostic_cfo_qam_v0_6(text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_exact_recording_receiver_agnostic_cfo_qam_v0_6(text,text,text,text),public.read_latest_recording_receiver_agnostic_cfo_qam_v0_6(text) TO leo_dashboard;

COMMIT;
