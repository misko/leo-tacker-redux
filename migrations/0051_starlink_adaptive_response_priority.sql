BEGIN;

-- Fresh captures remain ordinary priority zero.  Explicit exact-CAS
-- reanalysis (for a regression canary or changed plan) receives bounded
-- priority so a continuously arriving newest-first workload cannot starve it.
ALTER TABLE public.starlink_adaptive_response_work_v0_1
  ADD COLUMN priority smallint NOT NULL DEFAULT 0
  CHECK(priority BETWEEN 0 AND 100);

-- Migration 0050 may already have reopened exact rows before this priority
-- column existed.  Promote only that narrowly identified reanalysis state.
UPDATE public.starlink_adaptive_response_work_v0_1
SET priority=100
WHERE state IN('ready','failed')
  AND last_error LIKE 'analysis-plan-%';

DROP INDEX public.starlink_adaptive_response_work_claim_v0_1_idx;
CREATE INDEX starlink_adaptive_response_work_claim_v0_1_idx
  ON public.starlink_adaptive_response_work_v0_1(
    priority DESC,available_at_utc DESC,timeline_analysis_id DESC
  )
  WHERE state IN('ready','failed','leased');

CREATE OR REPLACE FUNCTION public.claim_starlink_adaptive_response_work_v0_1(text,interval)
RETURNS TABLE(timeline_analysis_id text,recording_id text,request_json jsonb,lease_token text,lease_generation bigint,attempt integer,source_suite_analysis_id text,source_suite_request_digest_value text,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 IF $1='' OR $2<interval '1 second' OR $2>interval '8 hours' THEN RAISE EXCEPTION 'invalid adaptive response claim' USING ERRCODE='22023'; END IF;
 RETURN QUERY WITH candidate AS (
   SELECT w.timeline_analysis_id,s.analysis_id AS suite_id
   FROM public.starlink_adaptive_response_work_v0_1 w
   JOIN LATERAL (SELECT x.analysis_id FROM public.recording_starlink_detector_suite x JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(x.bundle_digest_algorithm,x.bundle_digest_value) WHERE x.recording_id=w.recording_id AND x.result_state='candidates' AND o.lifecycle_state='live' ORDER BY x.published_at_utc DESC,x.analysis_id DESC LIMIT 1) s ON true
   WHERE w.attempt<8 AND w.available_at_utc<=clock_timestamp() AND (w.state IN('ready','failed') OR (w.state='leased' AND w.lease_expires_utc<=clock_timestamp()))
   ORDER BY w.priority DESC,w.available_at_utc DESC,w.timeline_analysis_id DESC FOR UPDATE OF w SKIP LOCKED LIMIT 1
 ), claimed AS (
   UPDATE public.starlink_adaptive_response_work_v0_1 w SET state='leased',attempt=w.attempt+1,lease_generation=w.lease_generation+1,lease_token=$1,lease_expires_utc=clock_timestamp()+$2,last_error=NULL FROM candidate c WHERE w.timeline_analysis_id=c.timeline_analysis_id RETURNING w.*,c.suite_id
 )
 SELECT c.timeline_analysis_id,c.recording_id,c.request_json,c.lease_token,c.lease_generation,c.attempt,s.analysis_id,s.request_digest_value,s.bundle_digest_algorithm,s.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator
 FROM claimed c JOIN public.recording_starlink_detector_suite s ON s.analysis_id=c.suite_id JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value);
END $function$;

CREATE OR REPLACE FUNCTION public.requeue_starlink_adaptive_response_work_v0_1(text,text,text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $function$
WITH changed AS (
  UPDATE public.starlink_adaptive_response_work_v0_1 w
  SET state='ready',
      priority=100,
      available_at_utc=clock_timestamp(),
      attempt=0,
      result_analysis_id=NULL,
      result_bundle_digest_value=NULL,
      completed_at_utc=NULL,
      last_error=$3
  WHERE w.recording_id=$1
    AND w.state='succeeded'
    AND w.result_analysis_id=$2
    AND $3~'^[a-z0-9][a-z0-9._:-]{0,127}$'
    AND EXISTS (
      SELECT 1
      FROM public.recording_starlink_adaptive_response_v0_1 r
      WHERE r.analysis_id=$2 AND r.recording_id=$1
    )
  RETURNING 1
)
SELECT count(*)=1 FROM changed;
$function$;

COMMIT;
