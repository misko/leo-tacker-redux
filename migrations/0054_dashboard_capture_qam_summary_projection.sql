BEGIN;

CREATE TABLE public.dashboard_capture_qam_candidate_v0_1 (
  source_kind text NOT NULL CHECK(source_kind IN ('acquired-v0.3','adaptive-v0.4')),
  analysis_id text NOT NULL,
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  radio_id text NOT NULL,
  lnb_id text NOT NULL CHECK(lnb_id<>''),
  receiver_chain_id text NOT NULL,
  segment_id text NOT NULL,
  edge text NOT NULL CHECK(edge IN ('lower','upper')),
  qam_goodness double precision NOT NULL CHECK(qam_goodness BETWEEN 0 AND 1 AND qam_goodness<>'NaN'::double precision),
  hard_symbol_accuracy double precision NOT NULL CHECK(hard_symbol_accuracy BETWEEN 0 AND 1 AND hard_symbol_accuracy<>'NaN'::double precision),
  rms_evm double precision NOT NULL CHECK(rms_evm>=0 AND rms_evm<>'NaN'::double precision),
  window_count integer NOT NULL CHECK(window_count BETWEEN 1 AND 32),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  PRIMARY KEY(source_kind,analysis_id,radio_id,lnb_id,receiver_chain_id),
  UNIQUE(source_kind,analysis_id,radio_id,receiver_chain_id)
);
CREATE INDEX dashboard_capture_qam_candidate_recording_idx
  ON public.dashboard_capture_qam_candidate_v0_1(recording_id,source_kind,published_at_utc DESC,analysis_id DESC);

CREATE FUNCTION public.publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE kind alias for $1; aid alias for $2; candidates alias for $3; item jsonb; expected integer; source_recording text;
BEGIN
  IF kind NOT IN ('acquired-v0.3','adaptive-v0.4') OR jsonb_typeof(candidates)<>'array'
     OR jsonb_array_length(candidates) NOT BETWEEN 1 AND 16 THEN
    RAISE EXCEPTION 'invalid QAM summary projection' USING ERRCODE='22023';
  END IF;
  IF kind='acquired-v0.3' THEN
    SELECT recording_id INTO source_recording FROM public.recording_starlink_acquired_constellation_v0_3 WHERE analysis_id=aid;
  ELSE
    SELECT recording_id INTO source_recording FROM public.recording_starlink_adaptive_qam_v0_4 WHERE analysis_id=aid;
  END IF;
  IF source_recording IS NULL THEN RAISE EXCEPTION 'QAM summary source is not cataloged' USING ERRCODE='23503'; END IF;
  expected := jsonb_array_length(candidates);
  FOR item IN SELECT value FROM jsonb_array_elements(candidates) LOOP
    IF jsonb_typeof(item)<>'object' OR item->>'analysis_id'<>aid OR item->>'source_kind'<>kind
       OR item->>'recording_id'<>source_recording
       OR (item->>'qam_goodness')::double precision NOT BETWEEN 0 AND 1
       OR (item->>'hard_symbol_accuracy')::double precision NOT BETWEEN 0 AND 1
       OR (item->>'qam_goodness')::double precision='NaN'::double precision
       OR (item->>'hard_symbol_accuracy')::double precision='NaN'::double precision
       OR (item->>'rms_evm')::double precision < 0
       OR (item->>'rms_evm')::double precision='NaN'::double precision
       OR (item->>'window_count')::integer NOT BETWEEN 1 AND 32 THEN
      RAISE EXCEPTION 'invalid QAM summary candidate' USING ERRCODE='22023';
    END IF;
    INSERT INTO public.dashboard_capture_qam_candidate_v0_1(
      source_kind,analysis_id,recording_id,radio_id,lnb_id,receiver_chain_id,
      segment_id,edge,qam_goodness,hard_symbol_accuracy,rms_evm,window_count
    ) VALUES(
      kind,aid,item->>'recording_id',item->>'radio_id',item->>'lnb_id',
      item->>'receiver_chain_id',item->>'segment_id',item->>'edge',
      (item->>'qam_goodness')::double precision,
      (item->>'hard_symbol_accuracy')::double precision,
      (item->>'rms_evm')::double precision,(item->>'window_count')::integer
    ) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM public.dashboard_capture_qam_candidate_v0_1 c
     WHERE c.source_kind=kind AND c.analysis_id=aid
       AND c.recording_id=item->>'recording_id' AND c.radio_id=item->>'radio_id'
       AND c.lnb_id=item->>'lnb_id' AND c.receiver_chain_id=item->>'receiver_chain_id'
       AND c.segment_id=item->>'segment_id' AND c.edge=item->>'edge'
       AND c.qam_goodness=(item->>'qam_goodness')::double precision
       AND c.hard_symbol_accuracy=(item->>'hard_symbol_accuracy')::double precision
       AND c.rms_evm=(item->>'rms_evm')::double precision
       AND c.window_count=(item->>'window_count')::integer;
    IF NOT FOUND THEN RAISE EXCEPTION 'QAM summary projection conflicts' USING ERRCODE='23505'; END IF;
  END LOOP;
  IF (SELECT count(*) FROM public.dashboard_capture_qam_candidate_v0_1
       WHERE source_kind=kind AND analysis_id=aid) <> expected THEN
    RAISE EXCEPTION 'QAM summary projection conflicts' USING ERRCODE='23505';
  END IF;
  RETURN true;
END $function$;

CREATE FUNCTION public.read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer)
RETURNS TABLE(
  recording_id text,radio_id text,analysis_state text,assignment_count bigint,
  source_kind text,analysis_id text,lnb_id text,receiver_chain_id text,
  segment_id text,edge text,qam_goodness double precision,
  hard_symbol_accuracy double precision,rms_evm double precision,
  window_count integer,original_recording_count bigint
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH latest_batches AS (
  SELECT DISTINCT ON (batch_id) projection_sequence,batch_id,requested_start_utc_ns
    FROM public.dashboard_capture_batch_projection
   WHERE requested_start_utc_ns >= $1 AND requested_start_utc_ns < $2
   ORDER BY batch_id,projection_sequence DESC
), successful AS (
  SELECT DISTINCT ON (attempt.recording_id) attempt.recording_id,attempt.radio_id,
         attempt.analysis_state,attempt.observed_start_utc_ns,
         batch.requested_start_utc_ns
    FROM latest_batches batch
    JOIN public.dashboard_capture_attempt_projection attempt
      ON attempt.projection_sequence=batch.projection_sequence
   WHERE attempt.capture_state='succeeded' AND attempt.recording_id IS NOT NULL
   ORDER BY attempt.recording_id,batch.requested_start_utc_ns DESC
), bounded AS (
  SELECT successful.*,count(*) OVER() AS original_recording_count
    FROM successful
   ORDER BY requested_start_utc_ns DESC,recording_id
   LIMIT $3
), assignments AS (
  SELECT bounded.recording_id,bounded.radio_id,bounded.analysis_state,
         bounded.original_recording_count,chain.receiver_chain_id,chain.lnb_id
    FROM bounded
    LEFT JOIN public.recording_hardware_link link ON link.recording_id=bounded.recording_id
    LEFT JOIN public.hardware_receiver_chain chain
      ON chain.snapshot_id=link.hardware_snapshot_id
     AND chain.radio_id=bounded.radio_id
     AND chain.valid_from_utc_ns<=bounded.observed_start_utc_ns
     AND (chain.valid_until_utc_ns IS NULL OR bounded.observed_start_utc_ns<chain.valid_until_utc_ns)
), selected_analysis AS (
  SELECT DISTINCT ON (recording_id) recording_id,source_kind,analysis_id
    FROM public.dashboard_capture_qam_candidate_v0_1
   ORDER BY recording_id,(source_kind='adaptive-v0.4') DESC,published_at_utc DESC,analysis_id DESC
), rows AS (
  SELECT a.recording_id,a.radio_id,a.analysis_state,a.original_recording_count,
         count(a.receiver_chain_id) OVER(PARTITION BY a.recording_id) AS assignment_count,
         c.source_kind,c.analysis_id,c.lnb_id,c.receiver_chain_id,c.segment_id,c.edge,
         c.qam_goodness,c.hard_symbol_accuracy,c.rms_evm,c.window_count
    FROM assignments a
    LEFT JOIN selected_analysis s ON s.recording_id=a.recording_id
    LEFT JOIN public.dashboard_capture_qam_candidate_v0_1 c
      ON c.recording_id=a.recording_id AND c.source_kind=s.source_kind
     AND c.analysis_id=s.analysis_id AND c.radio_id=a.radio_id
     AND c.receiver_chain_id=a.receiver_chain_id AND c.lnb_id=a.lnb_id
)
SELECT recording_id,radio_id,analysis_state,assignment_count,source_kind,analysis_id,
       lnb_id,receiver_chain_id,segment_id,edge,qam_goodness,hard_symbol_accuracy,
       rms_evm,window_count,original_recording_count
  FROM rows ORDER BY recording_id,lnb_id,receiver_chain_id;
$function$;

ALTER TABLE public.dashboard_capture_qam_candidate_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.dashboard_capture_qam_candidate_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.dashboard_capture_qam_candidate_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb),public.read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_dashboard_capture_qam_candidates_v0_1(text,text,jsonb) TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_dashboard_capture_qam_summaries_v0_1(bigint,bigint,integer) TO leo_dashboard;

COMMIT;
