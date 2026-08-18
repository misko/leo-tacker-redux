BEGIN;

CREATE TABLE public.dashboard_capture_qam_summary_receipt_v0_2 (
  source_kind text NOT NULL CHECK(source_kind IN ('acquired-v0.3','adaptive-v0.4')),
  analysis_id text NOT NULL,
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  source_request_digest_algorithm text NOT NULL CHECK(source_request_digest_algorithm='sha256'),
  source_request_digest_value text NOT NULL CHECK(source_request_digest_value~'^[0-9a-f]{64}$'),
  source_product_digest_algorithm text NOT NULL CHECK(source_product_digest_algorithm='sha256'),
  source_product_digest_value text NOT NULL CHECK(source_product_digest_value~'^[0-9a-f]{64}$'),
  summary_config_digest_algorithm text NOT NULL CHECK(summary_config_digest_algorithm='sha256'),
  summary_config_digest_value text NOT NULL CHECK(summary_config_digest_value~'^[0-9a-f]{64}$'),
  candidate_set_digest_algorithm text NOT NULL CHECK(candidate_set_digest_algorithm='sha256'),
  candidate_set_digest_value text NOT NULL CHECK(candidate_set_digest_value~'^[0-9a-f]{64}$'),
  terminal_outcome text NOT NULL CHECK(terminal_outcome IN ('complete','no-candidate')),
  candidate_count integer NOT NULL CHECK(candidate_count BETWEEN 0 AND 16),
  candidate_only boolean NOT NULL CHECK(candidate_only),
  calibration_required boolean NOT NULL CHECK(calibration_required),
  published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  PRIMARY KEY(source_kind,analysis_id),
  CHECK(
    (terminal_outcome='complete' AND candidate_count BETWEEN 1 AND 16)
    OR (terminal_outcome='no-candidate' AND candidate_count=0)
  ),
  CHECK(
    (source_kind='acquired-v0.3' AND analysis_id~'^slqam3rec_[0-9a-f]{32}$')
    OR (source_kind='adaptive-v0.4' AND analysis_id~'^slqam4_[0-9a-f]{32}$')
  )
);
CREATE INDEX dashboard_capture_qam_summary_receipt_recording_v0_2_idx
  ON public.dashboard_capture_qam_summary_receipt_v0_2(
    recording_id,published_at_utc DESC,analysis_id DESC
  );

CREATE FUNCTION public.publish_dashboard_capture_qam_summary_receipt_v0_2(
  text,text,text,text,text,jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE
  kind alias for $1;
  aid alias for $2;
  config_digest alias for $3;
  candidate_digest alias for $4;
  outcome alias for $5;
  candidates alias for $6;
  expected integer;
  source_recording text;
  source_request_digest text;
  source_product_digest text;
  inserted text;
BEGIN
  IF kind NOT IN ('acquired-v0.3','adaptive-v0.4')
     OR config_digest<>'0edbfe0faec9485ee75409640ad001c7a1dd6e2dafa280debc4e94b575fb31a3'
     OR candidate_digest!~'^[0-9a-f]{64}$'
     OR outcome NOT IN ('complete','no-candidate')
     OR jsonb_typeof(candidates)<>'array'
     OR jsonb_array_length(candidates) NOT BETWEEN 0 AND 16 THEN
    RAISE EXCEPTION 'invalid QAM summary receipt publication' USING ERRCODE='22023';
  END IF;
  expected := jsonb_array_length(candidates);
  IF (outcome='complete' AND expected NOT BETWEEN 1 AND 16)
     OR (outcome='no-candidate' AND expected<>0) THEN
    RAISE EXCEPTION 'QAM summary terminal outcome conflicts with candidate count'
      USING ERRCODE='22023';
  END IF;

  IF kind='acquired-v0.3' THEN
    SELECT recording_id,request_digest_value,bundle_digest_value
      INTO source_recording,source_request_digest,source_product_digest
      FROM public.recording_starlink_acquired_constellation_v0_3
     WHERE analysis_id=aid;
  ELSE
    SELECT recording_id,request_digest_value,bundle_digest_value
      INTO source_recording,source_request_digest,source_product_digest
      FROM public.recording_starlink_adaptive_qam_v0_4
     WHERE analysis_id=aid;
  END IF;
  IF source_recording IS NULL THEN
    RAISE EXCEPTION 'QAM summary receipt source is not cataloged'
      USING ERRCODE='23503';
  END IF;

  IF expected>0 THEN
    PERFORM public.publish_dashboard_capture_qam_candidates_v0_1(
      kind,aid,candidates
    );
  END IF;
  IF (SELECT count(*) FROM public.dashboard_capture_qam_candidate_v0_1 c
       WHERE c.source_kind=kind AND c.analysis_id=aid)<>expected THEN
    RAISE EXCEPTION 'QAM summary receipt candidate closure conflicts'
      USING ERRCODE='23505';
  END IF;

  INSERT INTO public.dashboard_capture_qam_summary_receipt_v0_2(
    source_kind,analysis_id,recording_id,
    source_request_digest_algorithm,source_request_digest_value,
    source_product_digest_algorithm,source_product_digest_value,
    summary_config_digest_algorithm,summary_config_digest_value,
    candidate_set_digest_algorithm,candidate_set_digest_value,
    terminal_outcome,candidate_count,candidate_only,calibration_required
  ) VALUES(
    kind,aid,source_recording,
    'sha256',source_request_digest,'sha256',source_product_digest,
    'sha256',config_digest,'sha256',candidate_digest,
    outcome,expected,true,true
  ) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
  IF inserted IS NOT NULL THEN RETURN true; END IF;

  PERFORM 1 FROM public.dashboard_capture_qam_summary_receipt_v0_2 r
   WHERE r.source_kind=kind AND r.analysis_id=aid
     AND r.recording_id=source_recording
     AND r.source_request_digest_algorithm='sha256'
     AND r.source_request_digest_value=source_request_digest
     AND r.source_product_digest_algorithm='sha256'
     AND r.source_product_digest_value=source_product_digest
     AND r.summary_config_digest_algorithm='sha256'
     AND r.summary_config_digest_value=config_digest
     AND r.candidate_set_digest_algorithm='sha256'
     AND r.candidate_set_digest_value=candidate_digest
     AND r.terminal_outcome=outcome AND r.candidate_count=expected
     AND r.candidate_only AND r.calibration_required;
  IF FOUND THEN RETURN true; END IF;
  RAISE EXCEPTION 'QAM summary receipt identity conflicts' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.read_exact_dashboard_capture_qam_summary_receipt_v0_2(
  text,text
) RETURNS TABLE(
  source_kind text,analysis_id text,recording_id text,
  source_request_digest_algorithm text,source_request_digest_value text,
  source_product_digest_algorithm text,source_product_digest_value text,
  summary_config_digest_algorithm text,summary_config_digest_value text,
  candidate_set_digest_algorithm text,candidate_set_digest_value text,
  terminal_outcome text,candidate_count integer,
  candidate_only boolean,calibration_required boolean
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
SELECT r.source_kind,r.analysis_id,r.recording_id,
       r.source_request_digest_algorithm,r.source_request_digest_value,
       r.source_product_digest_algorithm,r.source_product_digest_value,
       r.summary_config_digest_algorithm,r.summary_config_digest_value,
       r.candidate_set_digest_algorithm,r.candidate_set_digest_value,
       r.terminal_outcome,r.candidate_count,
       r.candidate_only,r.calibration_required
  FROM public.dashboard_capture_qam_summary_receipt_v0_2 r
 WHERE r.source_kind=$1 AND r.analysis_id=$2;
$function$;

CREATE FUNCTION public.read_pending_dashboard_capture_qam_products_v0_2(integer)
RETURNS TABLE(
  source_kind text,analysis_id text,recording_id text,
  source_request_digest_value text,source_product_digest_value text
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
WITH products AS (
  SELECT 'adaptive-v0.4'::text AS source_kind,analysis_id,recording_id,
         request_digest_value AS source_request_digest_value,
         bundle_digest_value AS source_product_digest_value,published_at_utc
    FROM public.recording_starlink_adaptive_qam_v0_4
  UNION ALL
  SELECT 'acquired-v0.3',analysis_id,recording_id,request_digest_value,
         bundle_digest_value,published_at_utc
    FROM public.recording_starlink_acquired_constellation_v0_3
)
SELECT p.source_kind,p.analysis_id,p.recording_id,
       p.source_request_digest_value,p.source_product_digest_value
  FROM products p
 WHERE $1 BETWEEN 1 AND 100
   AND NOT EXISTS(
     SELECT 1 FROM public.dashboard_capture_qam_summary_receipt_v0_2 receipt
      WHERE receipt.source_kind=p.source_kind AND receipt.analysis_id=p.analysis_id
   )
 ORDER BY p.published_at_utc DESC,p.analysis_id DESC
 LIMIT $1;
$function$;

ALTER TABLE public.dashboard_capture_qam_summary_receipt_v0_2 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.dashboard_capture_qam_summary_receipt_v0_2 TO leo_routine_owner;
REVOKE ALL ON public.dashboard_capture_qam_summary_receipt_v0_2
  FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_dashboard_capture_qam_summary_receipt_v0_2(
  text,text,text,text,text,jsonb
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_exact_dashboard_capture_qam_summary_receipt_v0_2(
  text,text
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_pending_dashboard_capture_qam_products_v0_2(integer)
  OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION
  public.publish_dashboard_capture_qam_summary_receipt_v0_2(text,text,text,text,text,jsonb),
  public.read_exact_dashboard_capture_qam_summary_receipt_v0_2(text,text),
  public.read_pending_dashboard_capture_qam_products_v0_2(integer)
  FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION
  public.publish_dashboard_capture_qam_summary_receipt_v0_2(text,text,text,text,text,jsonb),
  public.read_exact_dashboard_capture_qam_summary_receipt_v0_2(text,text),
  public.read_pending_dashboard_capture_qam_products_v0_2(integer)
  TO leo_analysis;
GRANT EXECUTE ON FUNCTION
  public.read_exact_dashboard_capture_qam_summary_receipt_v0_2(text,text)
  TO leo_dashboard;

COMMIT;
